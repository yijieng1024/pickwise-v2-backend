import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.database import get_session
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.laptops.brand_model import LaptopBrand
from app.laptops.pickscore_adapter import laptop_to_scorable, get_laptop_ranges
from app.laptops.pickscore_general import (
    USE_CASE_PRIORITIES,
    LaptopPickScore,
    generate_all_pick_scores,
    get_pick_scores_for_laptop,
    get_ranking_for_use_case,
)
from app.users.auth import get_current_admin, get_current_user
from app.users.models import LaptopUserPreference
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.pickscore.engine import calculate_pick_score
from app.pickscore.schemas import PickScoreResponse, BatchPickScoreResponse


class LaptopPickScoreRequest(BaseModel):
    laptop_id: uuid.UUID
    user_id: Optional[uuid.UUID] = None


class BatchLaptopPickScoreRequest(BaseModel):
    laptop_ids: List[uuid.UUID]
    user_id: Optional[uuid.UUID] = None


router = APIRouter(prefix="/laptops", tags=["Pick Score"])


def _fetch_shared_data(session: Session, user_id=None):
    ranges = get_laptop_ranges(session)
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]
    user_pref = None
    if user_id:
        user_pref = session.exec(
            select(LaptopUserPreference).where(LaptopUserPreference.user_id == user_id)
        ).first()
    return ranges, cpu_benchmarks, gpu_benchmarks, user_pref


def _resolve_laptop(session: Session, laptop_id: uuid.UUID) -> tuple[Laptop, str]:
    laptop = session.exec(select(Laptop).where(Laptop.id == laptop_id)).first()
    if not laptop:
        raise HTTPException(status_code=404, detail=f"Laptop {laptop_id} not found")
    brand = session.exec(select(LaptopBrand).where(LaptopBrand.id == laptop.brand_id)).first()
    return laptop, brand.name if brand else ""


class UseCasePickScore(BaseModel):
    use_case: str
    score: int
    breakdown: List[Dict[str, Any]]
    flags: Dict[str, Any]
    updated_at: datetime


class LaptopPickScoresResponse(BaseModel):
    laptop_id: uuid.UUID
    scores: List[UseCasePickScore]


@router.get("/{laptop_id}/pick-scores", response_model=LaptopPickScoresResponse)
def get_laptop_pick_scores(
    laptop_id: uuid.UUID,
    use_case: Optional[str] = Query(
        default=None,
        description=f"Filter to one use case. One of: {', '.join(USE_CASE_PRIORITIES)}",
    ),
    session: Session = Depends(get_session),
):
    """
    Public endpoint for the frontend's use-case cards: the precomputed
    general-mode PickScore of one laptop per use case (all five by default).
    Returns 404 for an unknown laptop and an empty list if scores haven't
    been generated yet (admin: POST /laptops/pick-scores/generate-all).
    """
    if use_case is not None and use_case not in USE_CASE_PRIORITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown use_case '{use_case}'. Valid: {', '.join(USE_CASE_PRIORITIES)}",
        )
    _resolve_laptop(session, laptop_id)  # 404 if the laptop doesn't exist

    rows = get_pick_scores_for_laptop(session, laptop_id, use_case)
    return LaptopPickScoresResponse(
        laptop_id=laptop_id,
        scores=[
            UseCasePickScore(
                use_case=r.use_case,
                score=r.score,
                breakdown=r.breakdown,
                flags=r.flags,
                updated_at=r.updated_at,
            )
            for r in sorted(rows, key=lambda r: list(USE_CASE_PRIORITIES).index(r.use_case))
        ],
    )


class RankedLaptopPickScore(BaseModel):
    rank: int
    laptop_id: uuid.UUID
    product_name: str
    brand_name: str
    price_rm: float
    image_urls: List[str]
    score: int
    flags: Dict[str, Any]


class UseCaseRankingResponse(BaseModel):
    use_case: str
    results: List[RankedLaptopPickScore]


@router.get("/pick-scores/ranking", response_model=UseCaseRankingResponse)
def get_use_case_ranking(
    use_case: str = Query(description=f"One of: {', '.join(USE_CASE_PRIORITIES)}"),
    limit: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
):
    """
    Public ranking for a use-case card: top laptops by stored general-mode
    PickScore, ties broken by lower price. Empty until scores are generated.
    """
    if use_case not in USE_CASE_PRIORITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown use_case '{use_case}'. Valid: {', '.join(USE_CASE_PRIORITIES)}",
        )
    rows = get_ranking_for_use_case(session, use_case, limit)
    return UseCaseRankingResponse(
        use_case=use_case,
        results=[
            RankedLaptopPickScore(
                rank=i + 1,
                laptop_id=laptop.id,
                product_name=laptop.product_name,
                brand_name=brand_name,
                price_rm=laptop.price_rm,
                image_urls=laptop.image_urls or [],
                score=score_row.score,
                flags=score_row.flags,
            )
            for i, (score_row, laptop, brand_name) in enumerate(rows)
        ],
    )


@router.get("/pick-scores/status")
def get_pick_score_status(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """
    How many laptops carry a stored PickScore vs. the catalog total.

    Mirrors GET /embeddings/laptops/status so the admin dashboard can render
    both coverage rails the same way. Without this, coverage was only
    knowable by re-running generate-all, which is a write.

    Counts DISTINCT laptop_id: the table holds one row per laptop × use case,
    so a raw row count would report several times the catalog size.
    """
    total_laptops = session.execute(select(func.count()).select_from(Laptop).where(Laptop.status == LaptopStatus.ACTIVE.value)).scalar() or 0
    scored = session.execute(
        select(func.count(func.distinct(LaptopPickScore.laptop_id)))
        .select_from(LaptopPickScore)
        .join(Laptop, Laptop.id == LaptopPickScore.laptop_id)
        .where(Laptop.status == LaptopStatus.ACTIVE.value)
    ).scalar() or 0

    return {
        "total_laptops": total_laptops,
        "scored": scored,
        "missing": total_laptops - scored,
        "coverage_pct": round(scored / total_laptops * 100, 1) if total_laptops > 0 else 0,
    }


@router.post("/pick-scores/generate-all", dependencies=[Depends(get_current_admin)])
def generate_all_laptop_pick_scores(session: Session = Depends(get_session)) -> Dict[str, Any]:
    """
    Admin: (re)compute the stored general-mode PickScores for every laptop ×
    use case. Deterministic and fast (no LLM) — run after processor imports,
    benchmark refreshes, or weight-profile changes.
    """
    return generate_all_pick_scores(session)


@router.post("/calculate-score", response_model=PickScoreResponse, dependencies=[Depends(get_current_user)])
def calculate_score(
    body: LaptopPickScoreRequest,
    session: Session = Depends(get_session),
):
    laptop, brand_name = _resolve_laptop(session, body.laptop_id)
    ranges, cpu_bm, gpu_bm, user_pref = _fetch_shared_data(session, body.user_id)
    product = laptop_to_scorable(laptop, brand_name)
    return calculate_pick_score(product, user_pref, ranges, cpu_bm, gpu_bm)


@router.post("/calculate-score/batch", response_model=BatchPickScoreResponse, dependencies=[Depends(get_current_user)])
def calculate_score_batch(
    body: BatchLaptopPickScoreRequest,
    session: Session = Depends(get_session),
):
    if not body.laptop_ids:
        return BatchPickScoreResponse(results=[])

    ranges, cpu_bm, gpu_bm, user_pref = _fetch_shared_data(session, body.user_id)

    results = []
    for laptop_id in body.laptop_ids:
        laptop, brand_name = _resolve_laptop(session, laptop_id)
        product = laptop_to_scorable(laptop, brand_name)
        results.append(calculate_pick_score(product, user_pref, ranges, cpu_bm, gpu_bm))

    return BatchPickScoreResponse(results=results)
