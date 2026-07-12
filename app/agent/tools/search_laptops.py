import uuid
from typing import Optional

from langchain_core.tools import tool
from sqlmodel import Session, select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.brand_model import LaptopBrand
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.pickscore.engine import calculate_pick_score
from app.rag.evaluation import log_pipeline_result
from app.rag.gating import relevance_gate
from app.rag.reranker import RankedCandidate, UserConstraints, rerank
from app.rag.relaxation import needs_relaxation, relax_and_retry
from app.rag.retrieval import retrieve_candidates
from app.database import engine
from app.logger import get_logger

logger = get_logger(__name__)

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


def _pick_scores_for(top: list[RankedCandidate], session: Session) -> dict[str, dict]:
    """
    General-mode PickScore (no user preference row — the agent has no user
    context) for the gated top results only. Same batch pattern as
    app/recommendation/service.py: ranges + benchmark tuples fetched once,
    reused for every laptop. Keyed by str(laptop_id).

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
        resp = calculate_pick_score(product, None, ranges, cpu_benchmarks, gpu_benchmarks)
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
        "display_size_inch": laptop.display_size_inch,
        "similarity_score": rc.similarity_score,
        "final_score": rc.final_score,
        "penalty_reasons": rc.penalty_reasons,
        "bonus_reasons": rc.bonus_reasons,
    }


@tool
def search_laptops(
    user_query: str,
    budget_max: Optional[float] = None,
    brand: Optional[str] = None,
    purpose: Optional[str] = None,
    top_k: int = 10,
) -> dict:
    """
    Search the PickWise laptop catalog with the full retrieve -> rerank ->
    relax -> gate precision pipeline (pgvector semantic search, corrected
    against explicit constraints).

    Args:
        user_query: The user's natural-language description of what they want
            (e.g. "lightweight laptop for programming and long battery life").
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
        - confidence: "high" if good matches were found, "low" otherwise.
        - bottleneck: "budget" | "weight_limit" | "general" | None — only set
          when confidence is "low". Explains what's blocking a good match.
        - message: a ready-to-use clarifying question when confidence is
          "low" — you MUST relay this (or your own paraphrase of it) to the
          user instead of saying nothing was found and stopping.
        - relaxation_notice: set when a constraint had to be auto-relaxed to
          find any viable results — mention this to the user.
    """
    constraints = UserConstraints(
        budget=budget_max,
        weight_limit=None,
        purpose=_normalize_purpose(purpose),
        brand_preferences=[brand.lower()] if brand else [],
    )

    with Session(engine) as session:
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

        if gate.status == "gated":
            return {
                "results": [],
                "confidence": "low",
                "bottleneck": gate.bottleneck,
                "message": gate.message,
                "relaxation_notice": None,
            }

        top = gate.candidates[:top_k]

        # PickScore failure must not take down the search itself — results
        # just go out unscored (pick_score stays absent, persisted as NULL).
        try:
            pick_scores = _pick_scores_for(top, session)
        except Exception:
            logger.exception("search_laptops: PickScore computation failed")
            pick_scores = {}

        results = []
        for rc in top:
            d = _to_result_dict(rc)
            d.update(pick_scores.get(d["laptop_id"], {}))
            results.append(d)

        return {
            "results": results,
            "confidence": "high",
            "bottleneck": None,
            "message": None,
            "relaxation_notice": relaxation.user_message if relaxation else None,
        }
