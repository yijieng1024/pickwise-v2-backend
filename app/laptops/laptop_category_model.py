import uuid
from sqlmodel import SQLModel, Field


class LaptopCategory(SQLModel, table=True):
    __tablename__ = "laptop_categories"  # type: ignore

    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", primary_key=True)
    category_id: uuid.UUID = Field(foreign_key="categories.id", primary_key=True)
