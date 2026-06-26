import uuid
from typing import List, Optional

from pydantic import BaseModel, Field

from app.pickscore.schemas import FactorBreakdown


class RecommendationRequest(BaseModel):
    query: str
    budget_max: Optional[float] = None
    brand: Optional[str] = None
    top_k: int = Field(default=3, ge=1, le=10)
    candidate_pool_size: int = Field(default=15, ge=5, le=30)


class RecommendedLaptop(BaseModel):
    laptop_id: uuid.UUID
    product_name: str
    price_rm: float
    pick_score: int
    similarity_score: float
    breakdown: List[FactorBreakdown]
    explanation: str


class RecommendationResponse(BaseModel):
    query: str
    recommendations: List[RecommendedLaptop]
    summary: str


# Internal schema for Gemini structured output — not exposed in the API
class _PerLaptopExplanation(BaseModel):
    laptop_id: str
    explanation: str


class _LLMOutput(BaseModel):
    explanations: List[_PerLaptopExplanation]
    summary: str
