from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from typing import List

from app.database import get_session
from app.taxonomy.product_type_model import ProductType
from app.users.questionnaire_model import QuestionnaireQuestion, QuestionnaireQuestionRead

router = APIRouter(prefix="/questionnaire", tags=["Questionnaire"])


@router.get("", response_model=List[QuestionnaireQuestionRead])
def list_questionnaire(
    product_type: str = "laptop",
    session: Session = Depends(get_session),
):
    """Public — active questionnaire questions for a product type, ordered by step_order."""
    product_type_row = session.exec(
        select(ProductType).where(ProductType.name == product_type)
    ).first()

    if not product_type_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product type '{product_type}' not found",
        )

    return session.exec(
        select(QuestionnaireQuestion)
        .where(QuestionnaireQuestion.product_type_id == product_type_row.id)
        .where(QuestionnaireQuestion.is_active == True)  # noqa: E712
        .order_by(QuestionnaireQuestion.step_order)
    ).all()
