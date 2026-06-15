import uuid
from typing import Optional
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field

class LaptopBrand(SQLModel, table=True):
    __tablename__ = "laptop_brands"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True, index=True)
    base_scrape_url: str
    icons_url: Optional[str] = Field(nullable=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# Brand Schema Models for API
class BrandBase(SQLModel):
    name: str
    base_scrape_url: str
    icons_url: Optional[str] = None
    is_active: bool = Field(default=True)

class BrandCreate(BrandBase):
    pass

class BrandUpdate(SQLModel):
    name: Optional[str] = None
    base_scrape_url: Optional[str] = None
    icons_url: Optional[str] = None
    is_active: Optional[bool] = None

class BrandRead(BrandBase):
    id: uuid.UUID
    created_at: datetime