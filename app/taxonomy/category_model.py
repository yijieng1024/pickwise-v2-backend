import uuid
from typing import TYPE_CHECKING, List, Optional
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Relationship

from app.laptops.laptop_category_model import LaptopCategory

if TYPE_CHECKING:
    from app.laptops.laptop_models import Laptop
    from app.laptops.customization_model import LaptopCustomization


class Category(SQLModel, table=True):
    __tablename__ = "categories"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True, index=True)
    icon_url: Optional[str] = Field(default=None, nullable=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    laptops: List["Laptop"] = Relationship(back_populates="categories", link_model=LaptopCategory)
    customizations: List["LaptopCustomization"] = Relationship(back_populates="category")


class CategoryBase(SQLModel):
    name: str
    icon_url: Optional[str] = None
    is_active: bool = Field(default=True)


class CategoryCreate(CategoryBase):
    pass


class CategoryUpdate(SQLModel):
    name: Optional[str] = None
    icon_url: Optional[str] = None
    is_active: Optional[bool] = None


class CategoryRead(CategoryBase):
    id: uuid.UUID
    created_at: datetime
