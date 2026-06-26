from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.users.auth import get_current_user
from app.users.models import User
from app.recommendation import service
from app.recommendation.schemas import RecommendationRequest, RecommendationResponse

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])


@router.post("/laptops", response_model=RecommendationResponse)
def recommend_laptops(
    request: RecommendationRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return service.get_recommendations(
        query=request.query,
        user_id=current_user.id,
        session=session,
        budget_max=request.budget_max,
        brand=request.brand,
        top_k=request.top_k,
        candidate_pool_size=request.candidate_pool_size,
    )
