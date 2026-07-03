import uuid
from typing import Optional
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field


class ProductType(SQLModel, table=True):
    __tablename__ = "product_types"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True, index=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProductTypeBase(SQLModel):
    name: str
    is_active: bool = Field(default=True)


class ProductTypeCreate(ProductTypeBase):
    pass


class ProductTypeUpdate(SQLModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class ProductTypeRead(ProductTypeBase):
    id: uuid.UUID
    created_at: datetime
