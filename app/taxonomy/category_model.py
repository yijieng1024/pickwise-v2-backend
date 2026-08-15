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

    # overlaps: Laptop.category_links maps the same junction table directly (it
    # exists so deleting a laptop cascades its laptop_categories rows away).
    # Both writing laptop_categories.laptop_id is intentional, so declare the
    # overlap rather than let SQLAlchemy warn about it on every configure.
    laptops: List["Laptop"] = Relationship(
        back_populates="categories",
        link_model=LaptopCategory,
        sa_relationship_kwargs={"overlaps": "category_links"},
    )
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


# Deferred import so this module can be an entry point on its own — Category's
# relationships name "Laptop"/"LaptopCustomization" as strings and SQLAlchemy
# resolves those only against imported classes. Bottom of the file, after
# Category exists, because laptop_models imports it straight back.
from app.laptops.laptop_models import Laptop  # noqa: E402,F401
