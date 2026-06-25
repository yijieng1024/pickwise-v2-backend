import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.laptops.brand_model import LaptopBrand
from app.laptops.pickscore_adapter import laptop_to_scorable, get_laptop_ranges
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


@router.post("/calculate-score", response_model=PickScoreResponse)
def calculate_score(
    body: LaptopPickScoreRequest,
    session: Session = Depends(get_session),
):
    laptop, brand_name = _resolve_laptop(session, body.laptop_id)
    ranges, cpu_bm, gpu_bm, user_pref = _fetch_shared_data(session, body.user_id)
    product = laptop_to_scorable(laptop, brand_name)
    return calculate_pick_score(product, user_pref, ranges, cpu_bm, gpu_bm)


@router.post("/calculate-score/batch", response_model=BatchPickScoreResponse)
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
