import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel

from app.pickscore.schemas import FactorBreakdown


class ChatRequest(BaseModel):
    message: str


class RecommendedLaptopInChat(BaseModel):
    laptop_id: uuid.UUID
    product_name: str
    price_rm: float
    pick_score: int
    similarity_score: float
    final_score: float
    breakdown: List[FactorBreakdown]
    explanation: str


class ChatResponse(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str                                    # main text shown in chat bubble
    recommendations: List[RecommendedLaptopInChat] = []  # populated on new search pass
    gate_status: Literal["pass", "gated"]
    relaxation_notice: Optional[str] = None         # shown when a constraint was relaxed
    conversation_id: uuid.UUID
