import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint


class SavedLaptop(SQLModel, table=True):
    """A user's saved/wishlisted laptop. One row per (user, laptop)."""

    __tablename__ = "saved_laptops"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("user_id", "laptop_id", name="uq_saved_laptops_user_laptop"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
