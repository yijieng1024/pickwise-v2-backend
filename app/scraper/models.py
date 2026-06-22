from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime, timezone
from uuid import UUID, uuid4
import uuid
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from typing import List, Dict, Any


class ScrapeTarget(SQLModel, table=True):
    __tablename__ = "laptop_scrape_urls"  # type: ignore

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    url: str = Field(unique=True, index=True)
    brand_id: UUID = Field(foreign_key="laptop_brands.id")
    last_scraped_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # status for the website
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class RawScrapLaptop(SQLModel, table=True):
    __tablename__ = "raw_scrap_laptops"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_url: str = Field(unique=True, index=True)
    brand_id: uuid.UUID = Field(foreign_key="laptop_brands.id")
    raw_product_name: str
    raw_prices: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    image_urls: List[str] = Field(default_factory=list, sa_column=Column(JSONB))

    raw_specs_dump: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB)
    )

    # AI Queue Tracker
    processing_status: str = Field(
        default="pending"
    )  # States: 'pending', 'processing', 'completed', 'failed'

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))