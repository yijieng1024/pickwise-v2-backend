import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class PickScoreRequest(BaseModel):
    laptop_id: uuid.UUID
    user_id: Optional[uuid.UUID] = None  # null triggers general mode


class FactorBreakdown(BaseModel):
    factor: str
    raw_score: float
    weight: float
    contribution: float


class PickScoreResponse(BaseModel):
    laptop_id: uuid.UUID
    score: int
    mode: str
    breakdown: List[FactorBreakdown]
    flags: Dict[str, Any]


class BatchPickScoreRequest(BaseModel):
    laptop_ids: List[uuid.UUID]
    user_id: Optional[uuid.UUID] = None


class BatchPickScoreResponse(BaseModel):
    results: List[PickScoreResponse]
