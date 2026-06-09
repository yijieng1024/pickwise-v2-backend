from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime, timezone
from uuid import UUID, uuid4


class ScrapeTarget(SQLModel, table=True):
    __tablename__ = "laptop_scrape_urls"  # type: ignore

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    url: str = Field(unique=True, index=True)
    brand_id: UUID = Field(foreign_key="laptop_brands.id")
    last_scraped_at: Optional[datetime] = Field(default=None)
    # status for the website
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
