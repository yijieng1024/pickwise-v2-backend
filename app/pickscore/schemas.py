import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


@dataclass
class ScorableProduct:
    """Product-agnostic input contract for the PickScore engine."""
    product_id: uuid.UUID
    brand_name: str
    price: float
    cpu_model: str
    gpu_model: str
    ram_gb: int
    storage_gb: int
    storage_type: Optional[str]
    weight_kg: float
    battery_wh: float
    display_size_inch: float


class FactorBreakdown(BaseModel):
    factor: str
    raw_score: float
    weight: float
    contribution: float


class PickScoreResponse(BaseModel):
    product_id: uuid.UUID
    score: int
    mode: str
    breakdown: List[FactorBreakdown]
    flags: Dict[str, Any]


class BatchPickScoreResponse(BaseModel):
    results: List[PickScoreResponse]
