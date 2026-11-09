"""
`user_background`: the onboarding background answers, one row per user (decided
2026-11-09). Every answer is optional and saved on its own, so a row with all
answers null is valid.

Kept out of `users` so `get_current_user`, which loads a User on every request,
does not carry it, and out of `laptop_user_preference`, which is about the laptop
the user wants, not who they are. Knowledge level is not here: it is the existing
`laptop_user_preference.tech_savviness`.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field as PydanticField, field_validator
from sqlalchemy import Column, DateTime, Text
from sqlmodel import Field, SQLModel

USER_TYPES = ("student", "working", "buying_for_business", "other")
ANSWERS = ("user_type", "field_of_study", "current_laptop", "change_reason")


class UserBackground(SQLModel, table=True):
    __tablename__ = "user_background"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", unique=True)
    # Plain VARCHAR, validated in BackgroundUpdate: a new value needs no ALTER TYPE.
    user_type: Optional[str] = Field(default=None, max_length=20)
    field_of_study: Optional[str] = Field(default=None, max_length=100)
    current_laptop: Optional[str] = Field(default=None, max_length=120)
    change_reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class BackgroundRead(BaseModel):
    user_type: Optional[str] = None
    field_of_study: Optional[str] = None
    current_laptop: Optional[str] = None
    change_reason: Optional[str] = None


class BackgroundUpdate(BaseModel):
    """PUT body. Only the fields sent change; an explicit null clears one."""
    model_config = ConfigDict(extra="forbid")

    user_type: Optional[str] = None
    field_of_study: Optional[str] = PydanticField(default=None, max_length=100)
    current_laptop: Optional[str] = PydanticField(default=None, max_length=120)
    change_reason: Optional[str] = PydanticField(default=None, max_length=500)

    @field_validator("user_type")
    @classmethod
    def known_user_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in USER_TYPES:
            raise ValueError(f"user_type must be one of {', '.join(USER_TYPES)}")
        return v

    @field_validator("field_of_study", "current_laptop", "change_reason")
    @classmethod
    def blank_is_null(cls, v: Optional[str]) -> Optional[str]:
        return (v.strip() or None) if v is not None else None


def apply_update(row: UserBackground, update: BackgroundUpdate) -> UserBackground:
    """Pure: set only the fields the client sent. Field of study is asked only of
    students, so leaving `student` clears it rather than keep an answer the user
    can no longer see or edit."""
    for key, value in update.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    if row.user_type != "student":
        row.field_of_study = None
    row.updated_at = datetime.now(timezone.utc)
    return row


def to_read(row: Optional[UserBackground]) -> BackgroundRead:
    return BackgroundRead(**{k: getattr(row, k) for k in ANSWERS}) if row else BackgroundRead()
