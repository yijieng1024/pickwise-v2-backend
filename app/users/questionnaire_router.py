from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from typing import List, Optional
from uuid import UUID

from app.database import get_session
from app.taxonomy.product_type_model import ProductType
from app.users.auth import get_current_admin
from app.users.questionnaire_model import (
    QuestionnaireQuestion,
    QuestionnaireQuestionRead,
    QuestionnaireQuestionCreate,
    QuestionnaireQuestionUpdate,
)

router = APIRouter(prefix="/questionnaire", tags=["Questionnaire"])


def _check_step_conflict(
    session: Session,
    product_type_id: UUID,
    step_order: int,
    exclude_id: Optional[UUID] = None,
) -> None:
    """409 if another active question already occupies this step for the product type."""
    query = (
        select(QuestionnaireQuestion)
        .where(QuestionnaireQuestion.product_type_id == product_type_id)
        .where(QuestionnaireQuestion.step_order == step_order)
        .where(QuestionnaireQuestion.is_active == True)  # noqa: E712
    )
    if exclude_id:
        query = query.where(QuestionnaireQuestion.id != exclude_id)

    if session.exec(query).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active question already exists at step {step_order} for this product type",
        )


@router.post("", response_model=QuestionnaireQuestionRead, dependencies=[Depends(get_current_admin)], status_code=201)
def create_question(
    question: QuestionnaireQuestionCreate,
    session: Session = Depends(get_session),
):
    """Create a new questionnaire question."""
    if not session.get(ProductType, question.product_type_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product type not found",
        )

    if question.is_active:
        _check_step_conflict(session, question.product_type_id, question.step_order)

    db_question = QuestionnaireQuestion.model_validate(question)
    session.add(db_question)
    session.commit()
    session.refresh(db_question)
    return db_question


@router.get("", response_model=List[QuestionnaireQuestionRead])
def list_questionnaire(
    product_type: str = "laptop",
    include_inactive: bool = False,
    session: Session = Depends(get_session),
):
    """Public — questionnaire questions for a product type, ordered by step_order.
    Active only by default; pass include_inactive=true for admin management views."""
    product_type_row = session.exec(
        select(ProductType).where(ProductType.name == product_type)
    ).first()

    if not product_type_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product type '{product_type}' not found",
        )

    query = select(QuestionnaireQuestion).where(
        QuestionnaireQuestion.product_type_id == product_type_row.id
    )
    if not include_inactive:
        query = query.where(QuestionnaireQuestion.is_active == True)  # noqa: E712

    return session.exec(query.order_by(QuestionnaireQuestion.step_order)).all()


@router.get("/{question_id}", response_model=QuestionnaireQuestionRead)
def get_question(question_id: UUID, session: Session = Depends(get_session)):
    """Get a specific questionnaire question by ID."""
    question = session.get(QuestionnaireQuestion, question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question not found"
        )
    return question


@router.put("/{question_id}", response_model=QuestionnaireQuestionRead, dependencies=[Depends(get_current_admin)])
def update_question(
    question_id: UUID,
    question_update: QuestionnaireQuestionUpdate,
    session: Session = Depends(get_session),
):
    """Update an existing questionnaire question."""
    db_question = session.get(QuestionnaireQuestion, question_id)
    if not db_question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question not found"
        )

    update_data = question_update.model_dump(exclude_unset=True)

    new_product_type_id = update_data.get("product_type_id", db_question.product_type_id)
    if new_product_type_id != db_question.product_type_id and not session.get(
        ProductType, new_product_type_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product type not found",
        )

    new_step_order = update_data.get("step_order", db_question.step_order)
    new_is_active = update_data.get("is_active", db_question.is_active)
    if new_is_active:
        _check_step_conflict(
            session, new_product_type_id, new_step_order, exclude_id=question_id
        )

    for key, value in update_data.items():
        setattr(db_question, key, value)

    session.add(db_question)
    session.commit()
    session.refresh(db_question)
    return db_question


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_question(question_id: UUID, session: Session = Depends(get_session)):
    """Delete a questionnaire question. Prefer setting is_active=false to retire
    a question while keeping it visible in admin views."""
    db_question = session.get(QuestionnaireQuestion, question_id)
    if not db_question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Question not found"
        )

    session.delete(db_question)
    session.commit()
    return None
