import uuid
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Column, LargeBinary


class UserAvatar(SQLModel, table=True):
    """Avatar image bytes, 1:1 with users. Kept out of the users table so the
    blob is never loaded by the per-request get_current_user User lookup."""
    __tablename__ = "user_avatars"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", unique=True, index=True)
    content_type: str
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
