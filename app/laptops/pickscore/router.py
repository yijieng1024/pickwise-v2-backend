from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.laptops.brand_model import LaptopBrand
from app.users.models import LaptopUserPreference
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.pickscore.schemas import (
    PickScoreRequest,
    PickScoreResponse,
    BatchPickScoreRequest,
    BatchPickScoreResponse,
)
from app.laptops.pickscore.ranges_cache import get_statistical_ranges
from app.laptops.pickscore.engine import calculate_pick_score

router = APIRouter(prefix="/laptops", tags=["Pick Score"])


def _fetch_prerequisites(session: Session, user_id=None):
    """Fetch shared data used across all score calculations in a request."""
    ranges = get_statistical_ranges(session)
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]
    user_pref = None
    if user_id:
        user_pref = session.exec(
            select(LaptopUserPreference).where(LaptopUserPreference.user_id == user_id)
        ).first()
    return ranges, cpu_benchmarks, gpu_benchmarks, user_pref


def _resolve_laptop_and_brand(session: Session, laptop_id) -> tuple[Laptop, str]:
    laptop = session.exec(select(Laptop).where(Laptop.id == laptop_id)).first()
    if not laptop:
        raise HTTPException(status_code=404, detail=f"Laptop {laptop_id} not found")
    brand = session.exec(select(LaptopBrand).where(LaptopBrand.id == laptop.brand_id)).first()
    brand_name = brand.name if brand else ""
    return laptop, brand_name


@router.post("/calculate-score", response_model=PickScoreResponse)
def calculate_score(
    body: PickScoreRequest,
    session: Session = Depends(get_session),
):
    laptop, brand_name = _resolve_laptop_and_brand(session, body.laptop_id)
    ranges, cpu_benchmarks, gpu_benchmarks, user_pref = _fetch_prerequisites(session, body.user_id)
    return calculate_pick_score(laptop, brand_name, user_pref, ranges, cpu_benchmarks, gpu_benchmarks)


@router.post("/calculate-score/batch", response_model=BatchPickScoreResponse)
def calculate_score_batch(
    body: BatchPickScoreRequest,
    session: Session = Depends(get_session),
):
    if not body.laptop_ids:
        return BatchPickScoreResponse(results=[])

    ranges, cpu_benchmarks, gpu_benchmarks, user_pref = _fetch_prerequisites(session, body.user_id)

    results = []
    for laptop_id in body.laptop_ids:
        laptop, brand_name = _resolve_laptop_and_brand(session, laptop_id)
        result = calculate_pick_score(laptop, brand_name, user_pref, ranges, cpu_benchmarks, gpu_benchmarks)
        results.append(result)

    return BatchPickScoreResponse(results=results)
