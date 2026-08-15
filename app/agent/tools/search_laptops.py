import uuid
from typing import Optional

from langchain_core.tools import tool
from sqlmodel import Session, select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.brand_model import LaptopBrand
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.pickscore.engine import calculate_pick_score
from app.users.models import LaptopUserPreference
from app.rag.evaluation import log_pipeline_result
from app.rag.gating import relevance_gate
from app.rag.reranker import RankedCandidate, UserConstraints, rerank
from app.rag.relaxation import needs_relaxation, relax_and_retry
from app.rag.retrieval import retrieve_candidates
from app.database import engine
from app.logger import get_logger

logger = get_logger(__name__)

# Hard ceiling on how many laptops a single search emits into the agent's
# context. See the _run_search slice for why this is the main tokens-per-minute
# lever. Tune here (not per-call) if the Gemini quota changes.
_MAX_RESULTS = 6

# Keys reranker.py's purpose-signal dicts are keyed by. Anything that doesn't
# normalize to one of these falls back to "Office" — the neutral default —
# so the tool never passes an unrecognized purpose string downstream.
_KNOWN_PURPOSES = {"Gaming", "Creative", "Programming", "Office"}


def _normalize_purpose(purpose: Optional[str]) -> list[str]:
    if not purpose or not purpose.strip():
        return []
    normalized = purpose.strip().title()
    if normalized not in _KNOWN_PURPOSES:
        normalized = "Office"
    return [normalized]


def _pick_scores_for(
    top: list[RankedCandidate],
    session: Session,
    user_pref: Optional[LaptopUserPreference] = None,
) -> dict[str, dict]:
    """
    PickScore for the gated top results only — personalized when the caller
    passes the authenticated user's preference row, general-mode otherwise
    (the eval harness and signed-in users without a questionnaire row).
    Same batch pattern as app/recommendation/service.py: ranges + benchmark
    tuples fetched once, reused for every laptop. Keyed by str(laptop_id).

    Only the composite score and a compact top-3 factor summary are exposed —
    the full 8-factor breakdown would bloat the LLM context and overflow the
    eval harness's 3,000-char per-output truncation at top_k=10.
    """
    ranges = get_laptop_ranges(session)
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]

    brand_names: dict[uuid.UUID, str] = {}
    scores: dict[str, dict] = {}
    for rc in top:
        laptop = rc._laptop  # type: ignore[attr-defined]
        if laptop.brand_id not in brand_names:
            brand = session.get(LaptopBrand, laptop.brand_id)
            brand_names[laptop.brand_id] = brand.name if brand else ""
        product = laptop_to_scorable(laptop, brand_names[laptop.brand_id])
        resp = calculate_pick_score(product, user_pref, ranges, cpu_benchmarks, gpu_benchmarks)
        top_factors = sorted(resp.breakdown, key=lambda f: f.contribution, reverse=True)[:3]
        scores[str(laptop.id)] = {
            "pick_score": resp.score,
            "pick_score_top_factors": ", ".join(
                f"{f.factor} {f.raw_score:.0f}" for f in top_factors
            ),
        }
    return scores


def _to_result_dict(rc: RankedCandidate) -> dict:
    laptop = rc._laptop  # type: ignore[attr-defined]
    # final_score / penalty_reasons / bonus_reasons are internal reranker
    # diagnostics — the gate/relaxation stages read them off the RankedCandidate
    # object, never this dict, and no downstream consumer (router persistence,
    # frontend cards) uses them either. Kept out of the emitted payload so they
    # don't inflate the LLM context on every follow-up step (Gemini free-tier
    # tokens-per-minute is the binding limit).
    return {
        "laptop_id": str(laptop.id),
        "model_code": laptop.model_code,
        "product_name": rc.product_name,
        "price_rm": rc.price_rm,
        "processor_model": laptop.processor_model,
        "gpu_model": laptop.gpu_model,
        "ram_gb": laptop.ram_gb,
        "storage_gb": laptop.ssd_gb,
        "storage_type": laptop.storage_type,
        "weight_kg": laptop.weight_kg,
        "battery_wh": laptop.battery_wh,
        "display_size_inch": laptop.display_size_inch,
        "similarity_score": rc.similarity_score,
    }


_SEARCH_LAPTOPS_DOC = """
    Search the PickWise laptop catalog with the full retrieve -> rerank ->
    relax -> gate precision pipeline (pgvector semantic search, corrected
    against explicit constraints).

    Args:
        user_query: The user's natural-language description of what they want
            (e.g. "lightweight laptop for programming and long battery life").
        weight_max: Max weight in kg. Use when the user asks for something
                    light/portable (e.g. 1.5 for "thin and light"). May be
                    auto-relaxed in 0.2kg steps if nothing viable is found.
        budget_max: Maximum price in RM. Applied as a hard filter, but may be
            auto-relaxed in small steps if nothing viable is found.
        brand: Brand name (e.g. "Apple", "Asus"). Treated as a soft preference,
            not a hard filter — laptops from other brands can still appear if
            they otherwise match well.
        purpose: One of "Gaming", "Creative", "Programming", or "Office".
            Anything else is treated as "Office".
        top_k: Max number of results to return on a successful match.

    Returns:
        A dict with:
        - results: list of matching laptops (empty when confidence is "low").
          Each result carries pick_score (0-100 deterministic hardware/value
          score) and pick_score_top_factors (its top scoring factors).
        - score_mode: "personalized" when pick_score values are weighted by
          the user's saved preference profile, "general" otherwise.
        - confidence: "high" if good matches were found, "low" otherwise.
        - bottleneck: "budget" | "weight_limit" | "general" | None — only set
          when confidence is "low". Explains what's blocking a good match.
        - message: a ready-to-use clarifying question when confidence is
          "low" — you MUST relay this (or your own paraphrase of it) to the
          user instead of saying nothing was found and stopping.
        - relaxation_notice: set when a constraint had to be auto-relaxed to
          find any viable results — mention this to the user.
    """


def _run_search(
    user_query: str,
    budget_max: Optional[float] = None,
    weight_max: Optional[float] = None,
    brand: Optional[str] = None,
    purpose: Optional[str] = None,
    top_k: int = 10,
    user_id: Optional[uuid.UUID] = None,
) -> dict:
    constraints = UserConstraints(
        budget=budget_max,
        weight_limit=weight_max,
        purpose=_normalize_purpose(purpose),
        brand_preferences=[brand.lower()] if brand else [],
    )

    with Session(engine) as session:
        # user_id is bound by make_search_laptops() from the authenticated
        # request — never an LLM-visible argument (a model could fabricate it).
        user_pref = None
        if user_id is not None:
            user_pref = session.exec(
                select(LaptopUserPreference).where(LaptopUserPreference.user_id == user_id)
            ).first()

        candidates = retrieve_candidates(user_query, session, budget_max=budget_max)
        ranked = rerank(candidates, constraints)

        relaxation = None
        if needs_relaxation(ranked):
            relaxation = relax_and_retry(user_query, constraints, session)
            ranked = relaxation.candidates if relaxation else []

        gate = relevance_gate(ranked)

        try:
            log_pipeline_result(
                gate=gate,
                query=user_query,
                relaxation=relaxation,
                session=session,
            )
        except Exception:
            logger.exception("search_laptops: failed to log pipeline result")

        score_mode = "personalized" if user_pref else "general"

        if gate.status == "gated":
            return {
                "results": [],
                "score_mode": score_mode,
                "confidence": "low",
                "bottleneck": gate.bottleneck,
                "message": gate.message,
                "relaxation_notice": None,
            }

        # Hard ceiling regardless of the top_k the LLM requests: every result
        # is re-sent in context on each follow-up reasoning step, so this is
        # the single biggest lever on tokens-per-minute. Six is plenty for a
        # chat shortlist (the frontend renders these as cards).
        top = gate.candidates[: min(top_k, _MAX_RESULTS)]

        # PickScore failure must not take down the search itself — results
        # just go out unscored (pick_score stays absent, persisted as NULL).
        try:
            pick_scores = _pick_scores_for(top, session, user_pref)
        except Exception:
            logger.exception("search_laptops: PickScore computation failed")
            pick_scores = {}
            score_mode = "general"

        results = []
        for rc in top:
            d = _to_result_dict(rc)
            d.update(pick_scores.get(d["laptop_id"], {}))
            results.append(d)

        return {
            "results": results,
            "score_mode": score_mode,
            "confidence": "high",
            "bottleneck": None,
            "message": None,
            "relaxation_notice": relaxation.user_message if relaxation else None,
        }


def _make_search_fn(user_id: Optional[uuid.UUID]):
    def search_laptops(
        user_query: str,
        budget_max: Optional[float] = None,
        weight_max: Optional[float] = None,
        brand: Optional[str] = None,
        purpose: Optional[str] = None,
        top_k: int = 10,
    ) -> dict:
        return _run_search(user_query, budget_max, weight_max, brand, purpose, top_k, user_id)

    search_laptops.__doc__ = _SEARCH_LAPTOPS_DOC
    return search_laptops


# Module-level tool: general-mode scores. Used by ALL_TOOLS (and therefore the
# eval harness), and by requests with no usable preference row.
search_laptops = tool(_make_search_fn(None))


def make_search_laptops(user_id: Optional[uuid.UUID]):
    """Per-request search_laptops bound to the authenticated user, so
    pick_score comes out personalized when their preference row exists.
    Same tool name, docstring, and arg schema as the module-level tool —
    the LLM cannot tell them apart."""
    if user_id is None:
        return search_laptops
    return tool(_make_search_fn(user_id))
