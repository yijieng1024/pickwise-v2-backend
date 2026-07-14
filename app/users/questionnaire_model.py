import uuid
from typing import List, Optional
from datetime import datetime, timezone
from enum import Enum
from sqlmodel import SQLModel, Field, Column, JSON


class QuestionType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    RANKING = "ranking"


class QuestionnaireQuestion(SQLModel, table=True):
    __tablename__ = "questionnaire_questions"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    product_type_id: uuid.UUID = Field(foreign_key="product_types.id", index=True)
    step_order: int = Field(index=True)
    question_text: str
    question_type: QuestionType
    target_field: str
    options: Optional[List[dict]] = Field(default=None, sa_column=Column(JSON))
    help_text: Optional[str] = None
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QuestionnaireQuestionRead(SQLModel):
    id: uuid.UUID
    product_type_id: uuid.UUID
    step_order: int
    question_text: str
    question_type: QuestionType
    target_field: str
    options: Optional[List[dict]]
    help_text: Optional[str]
    is_active: bool
    created_at: datetime


class QuestionnaireQuestionCreate(SQLModel):
    product_type_id: uuid.UUID
    step_order: int
    question_text: str
    question_type: QuestionType
    target_field: str
    options: Optional[List[dict]] = None
    help_text: Optional[str] = None
    is_active: bool = True


class QuestionnaireQuestionUpdate(SQLModel):
    product_type_id: Optional[uuid.UUID] = None
    step_order: Optional[int] = None
    question_text: Optional[str] = None
    question_type: Optional[QuestionType] = None
    target_field: Optional[str] = None
    options: Optional[List[dict]] = None
    help_text: Optional[str] = None
    is_active: Optional[bool] = None
