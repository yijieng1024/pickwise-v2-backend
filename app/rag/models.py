import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlmodel import Column, Field, Relationship, SQLModel
from sqlalchemy import Text, JSON


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    title: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    messages: List["Message"] = Relationship(back_populates="conversation")
    conversation_laptops: List["ConversationLaptop"] = Relationship(back_populates="conversation")


class Message(SQLModel, table=True):
    __tablename__ = "messages"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", index=True)
    role: MessageRole
    content: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    conversation: Optional[Conversation] = Relationship(back_populates="messages")


class ConversationLaptop(SQLModel, table=True):
    __tablename__ = "conversation_laptops"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", index=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id")
    pick_score: Optional[int] = None
    similarity_score: Optional[float] = None
    added_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    conversation: Optional[Conversation] = Relationship(back_populates="conversation_laptops")


class PipelineEvalLog(SQLModel, table=True):
    """
    One row per live user pipeline run.
    Stores the signals needed to calibrate Module 4's threshold and spot
    quality regressions without needing hand-labelled ground truth.
    """
    __tablename__ = "pipeline_eval_logs"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    conversation_id: Optional[uuid.UUID] = Field(default=None, foreign_key="conversations.id", index=True)
    query: str = Field(sa_column=Column(Text))
    gate_status: str                            # "pass" | "gated"
    top_score: Optional[float] = None           # highest final_score after reranking
    relaxed_field: Optional[str] = None         # "budget" | "weight_limit" | None
    relaxed_from: Optional[float] = None        # original constraint value
    relaxed_to: Optional[float] = None          # relaxed constraint value
    bottleneck: Optional[str] = None            # populated when gated
    candidate_count: int = Field(default=0)     # laptops that passed the gate
    result_laptop_ids: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Pydantic response schemas ---

class ConversationCreate(SQLModel):
    title: Optional[str] = None


class MessageRead(SQLModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime


class ConversationSummary(SQLModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationRead(SQLModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    messages: List[MessageRead] = []
