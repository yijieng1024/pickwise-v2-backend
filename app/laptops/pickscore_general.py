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
from typing import Optional

from sqlalchemy import Column, JSON, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.pickscore.engine import DEFAULT_PRIORITY, calculate_pick_score

# Use-case weight profiles (same 1-10 scale as DEFAULT_PRIORITY's N-i rule).
# Keys are the API slugs the frontend sends/receives. screen_size and brand
# always score a neutral 50 in general mode, so their weights stay minimal.
USE_CASE_PRIORITIES: dict[str, dict[str, float]] = {
    "office_study": {  # basic tasks: cheap, portable, all-day battery
        "price": 9, "cpu": 6, "gpu": 2, "ram_storage": 5,
        "portability": 7, "battery": 8, "screen_size": 2, "brand": 1,
    },
    "programming": {  # compile/IDE workloads: CPU + RAM first, still mobile
        "price": 6, "cpu": 9, "gpu": 4, "ram_storage": 9,
        "portability": 6, "battery": 7, "screen_size": 2, "brand": 1,
    },
    "gaming": {  # GPU-bound, usually plugged in and stationary
        "price": 5, "cpu": 8, "gpu": 10, "ram_storage": 7,
        "portability": 1, "battery": 2, "screen_size": 3, "brand": 1,
    },
    "creative_work": {  # design/video/3D: GPU + RAM heavy, bigger screens help
        "price": 4, "cpu": 8, "gpu": 9, "ram_storage": 8,
        "portability": 3, "battery": 3, "screen_size": 3, "brand": 1,
    },
    "general_use": dict(DEFAULT_PRIORITY),  # mixed/casual: balanced N-i rule
}


class LaptopPickScore(SQLModel, table=True):
    __tablename__ = "laptop_pick_scores"  # type: ignore
    __table_args__ = (
        UniqueConstraint("laptop_id", "use_case", name="uq_laptop_pick_scores_laptop_use_case"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    use_case: str = Field(index=True)  # slug from USE_CASE_PRIORITIES
    score: int
    breakdown: list = Field(default_factory=list, sa_column=Column(JSON))
    flags: dict = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def generate_all_pick_scores(session: Session) -> dict:
    """
    (Re)compute and upsert the general-mode PickScore of every laptop for
    every use-case profile. Ranges and benchmark lists are fetched once and
    reused across the whole catalog (same pattern as the batch endpoint).
    """
    ranges = get_laptop_ranges(session)
    cpu_bm = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_bm = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]

    laptops = session.exec(select(Laptop)).all()
    brands = session.exec(select(LaptopBrand)).all()
    brand_map = {b.id: b.name for b in brands}

    existing_rows = session.exec(select(LaptopPickScore)).all()
    existing = {(r.laptop_id, r.use_case): r for r in existing_rows}

    created, updated = 0, 0
    for laptop in laptops:
        product = laptop_to_scorable(laptop, brand_map.get(laptop.brand_id, ""))
        for use_case, priorities in USE_CASE_PRIORITIES.items():
            result = calculate_pick_score(
                product, None, ranges, cpu_bm, gpu_bm, priority_override=priorities
            )
            breakdown = [fb.model_dump() for fb in result.breakdown]

            row = existing.get((laptop.id, use_case))
            if row:
                row.score = result.score
                row.breakdown = breakdown
                row.flags = result.flags
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                updated += 1
            else:
                session.add(
                    LaptopPickScore(
                        laptop_id=laptop.id,
                        use_case=use_case,
                        score=result.score,
                        breakdown=breakdown,
                        flags=result.flags,
                    )
                )
                created += 1

    session.commit()
    return {
        "laptops": len(laptops),
        "use_cases": list(USE_CASE_PRIORITIES.keys()),
        "rows_created": created,
        "rows_updated": updated,
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
    """
    stmt = (
        select(LaptopPickScore, Laptop, LaptopBrand.name)
        .join(Laptop, Laptop.id == LaptopPickScore.laptop_id)  # type: ignore[arg-type]
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)  # type: ignore[arg-type]
        .where(LaptopPickScore.use_case == use_case)
    )
    rows = [(row[0], row[1], row[2]) for row in session.exec(stmt).all()]

    demote_proxy = use_case == "gaming"
    rows.sort(
        key=lambda t: (
            bool(t[0].flags.get("gpu_score_is_proxy")) if demote_proxy else False,
            -t[0].score,
            t[1].price_rm,
        )
    )
    return rows[:limit]
