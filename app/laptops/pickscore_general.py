"""
Precomputed general-mode PickScores per use case.

The frontend shows five use-case cards (Office/Study, Programming/Development,
Gaming, Creative Work, General Use). Instead of recomputing benchmark fuzzy
matches on every page view, each laptop's general-mode score is computed once
per use case with a use-case weight profile and stored in laptop_pick_scores.
Scores are deterministic (no LLM, no user context) so they only change when
the catalog, benchmarks, or weight profiles do — regenerate via
POST /laptops/pick-scores/generate-all after processor/benchmark runs.
"""
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.family_service import deduplicate_by_family
# LaptopPickScore is declared in laptop_models (see the comment on the class)
# and re-exported here, so `from app.laptops.pickscore_general import
# LaptopPickScore` — what pickscore_router and alembic/env.py do — still works.
from app.laptops.laptop_models import Laptop, LaptopPickScore, LaptopStatus  # noqa: F401
from app.laptops.pickscore_adapter import get_scoring_data, laptop_to_scorable
from app.pickscore.engine import calculate_pick_score

if TYPE_CHECKING:
    from app.common.job_service import JobProgress

# Use-case weight profiles (1-10 base-weight scale; normalized to sum 1 in
# calculate_pick_score). Keys are the API slugs the frontend sends/receives.
# screen_size and brand always score a neutral 50 in general mode, so their
# weights stay low. general_use deliberately does NOT reuse the engine's
# N-i DEFAULT_PRIORITY: that rule gave GPU ~17% weight for daily use — an
# explicit all-rounder profile (price-sensitive, GPU minimal) is more
# defensible.
USE_CASE_PRIORITIES: dict[str, dict[str, float]] = {
    "office_study": {  # basic tasks: cheap, portable, all-day battery
        "price": 9, "cpu": 6, "gpu": 2, "ram_storage": 5,
        "portability": 7, "battery": 8, "screen_size": 2, "brand": 1,
    },
    "programming": {  # compile/IDE workloads: CPU + RAM heavy; screen real estate
        # and portability matter about equally for devs on the go
        "price": 6, "cpu": 9, "gpu": 3, "ram_storage": 9,
        "portability": 4, "battery": 6, "screen_size": 4, "brand": 1,
    },
    "gaming": {  # portable desktop: raw GPU/CPU and screen immersion are what
        # matter — price sensitivity is lower than the other use cases
        "price": 4, "cpu": 8, "gpu": 10, "ram_storage": 7,
        "portability": 1, "battery": 1, "screen_size": 4, "brand": 1,
    },
    "creative_work": {  # design/video/3D: performance, RAM, and a big screen come
        # first — a professional tool, so price sensitivity is low
        "price": 3, "cpu": 8, "gpu": 9, "ram_storage": 8,
        "portability": 3, "battery": 3, "screen_size": 5, "brand": 1,
    },
    "general_use": {  # balanced all-rounder: price-first, GPU minimal
        "price": 9, "cpu": 7, "gpu": 2, "ram_storage": 7,
        "portability": 6, "battery": 6, "screen_size": 4, "brand": 2,
    },
}


def generate_all_pick_scores(session: Session, progress: Optional["JobProgress"] = None) -> dict:
    """
    (Re)compute and upsert the general-mode PickScore of every laptop for
    every use-case profile. Ranges and benchmark lists are fetched once and
    reused across the whole catalog (same pattern as the batch endpoint).

    All-or-nothing: every score is computed first, then every row is written
    in ONE transaction. Scores are percentiles of the same run's ranges, so a
    half-written table would mix two runs; if the process dies (or the job is
    stopped) before the commit, Postgres rolls the write back and the previous
    scores stay complete.

    `progress` is the background-job handle (POST /pick-scores/generate-all);
    direct callers such as the integration tests pass nothing.
    """
    ranges, cpu_bm, gpu_bm = get_scoring_data(session, force_refresh=True)
    laptops = session.exec(select(Laptop).where(Laptop.status == LaptopStatus.ACTIVE.value)).all()
    brand_map = {b.id: b.name for b in session.exec(select(LaptopBrand)).all()}
    products = [(l.id, laptop_to_scorable(l, brand_map.get(l.brand_id, ""))) for l in laptops]
    # End the read transaction: the compute loop below is pure Python plus one
    # bookkeeping commit per laptop, and must not sit on an open transaction.
    session.commit()
    if progress:
        progress.set_total(len(products))

    computed = []
    withheld = 0
    for laptop_id, product in products:
        results = {
            use_case: calculate_pick_score(product, None, ranges, cpu_bm, gpu_bm, priority_override=priorities)
            for use_case, priorities in USE_CASE_PRIORITIES.items()
        }
        withheld += any(r.score is None for r in results.values())
        computed.append((laptop_id, results))
        if progress:
            progress.advance(item=str(laptop_id))
            if progress.cancel_requested:
                # Stopped before the write: nothing changed, old scores stand.
                return {"laptops": len(products), "laptops_computed": len(computed),
                        "rows_created": 0, "rows_updated": 0, "written": False}

    existing = {(r.laptop_id, r.use_case): r for r in session.exec(select(LaptopPickScore)).all()}
    created, updated = 0, 0
    now = datetime.now(timezone.utc)
    for laptop_id, results in computed:
        for use_case, result in results.items():
            breakdown = [fb.model_dump() for fb in result.breakdown]
            row = existing.get((laptop_id, use_case))
            if row:
                row.score = result.score
                row.breakdown = breakdown
                row.flags = result.flags
                row.updated_at = now
                session.add(row)
                updated += 1
            else:
                session.add(
                    LaptopPickScore(
                        laptop_id=laptop_id,
                        use_case=use_case,
                        score=result.score,
                        breakdown=breakdown,
                        flags=result.flags,
                    )
                )
                created += 1

    session.commit()
    return {
        "laptops": len(products),
        "laptops_scored": len(products) - withheld,
        "laptops_withheld": withheld,
        "use_cases": list(USE_CASE_PRIORITIES.keys()),
        "rows_created": created,
        "rows_updated": updated,
        "written": True,
    }


def get_pick_scores_for_laptop(
    session: Session, laptop_id: uuid.UUID, use_case: Optional[str] = None
) -> list[LaptopPickScore]:
    stmt = select(LaptopPickScore).where(LaptopPickScore.laptop_id == laptop_id)
    if use_case:
        stmt = stmt.where(LaptopPickScore.use_case == use_case)
    return list(session.exec(stmt).all())


def get_ranking_for_use_case(
    session: Session, use_case: str, limit: int = 10
) -> list[tuple[LaptopPickScore, Laptop, str]]:
    """Top laptops for one use case, highest stored score first (price breaks
    ties). Returns (score_row, laptop, brand_name) tuples for the router.

    Gaming exception: Apple GPUs are scored via CPU proxy (no PassMark data
    for ARM), which made M5 MacBooks outrank RTX 5090 machines. A proxied GPU
    score is not evidence of gaming performance, so for the gaming use case
    proxy-scored laptops sort after every real-benchmark laptop.

    One laptop per family: configurations of one machine score within a point
    or two of each other, so an undeduplicated top-10 was mostly one product
    listed ten times. The family's highest scorer represents it — there is no
    query budget here to pick a member against, unlike the agent's search.
    Laptops with no family are left ungrouped rather than guessed at.
    """
    stmt = (
        select(LaptopPickScore, Laptop, LaptopBrand.name)
        .join(Laptop, Laptop.id == LaptopPickScore.laptop_id)  # type: ignore[arg-type]
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)  # type: ignore[arg-type]
        .where(LaptopPickScore.use_case == use_case)
        .where(Laptop.status == LaptopStatus.ACTIVE.value)
    )
    # A withheld score has no position in an ordering, so it is omitted HERE
    # and only here (ADR-0016): the row still exists and GET /{id}/pick-scores
    # still returns it with a null score and the flag. A ranking answers "what
    # is best"; a laptop nobody could score has no answer to that question.
    # The sort below also does -score, which raises TypeError on a None.
    rows = [
        (row[0], row[1], row[2])
        for row in session.exec(stmt).all()
        if row[0].score is not None
    ]

    demote_proxy = use_case == "gaming"
    rows.sort(
        key=lambda t: (
            bool(t[0].flags.get("gpu_score_is_proxy")) if demote_proxy else False,
            -t[0].score,
            t[1].price_rm,
            # Closes the order. Sibling configurations routinely tie on all
            # three keys above, and without this the tail falls through to
            # database return order, which Postgres does not guarantee — so
            # the ranking (and which member survives deduplication below)
            # would drift between identical requests.
            str(t[1].id),
        )
    )

    # Before the slice, never after: deduplicating a top-10 that is already
    # cut would return three products, not ten.
    rows = deduplicate_by_family(rows, family_id_of=lambda t: t[1].family_id)
    return rows[:limit]
