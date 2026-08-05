from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime, timezone
from uuid import UUID, uuid4
import uuid
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from typing import List, Dict, Any


class ScrapeStatus:
    """
    Lifecycle values for `ScrapeTarget.scrape_status` (a plain string column, so
    adding a value needs no migration).

    Two routes reach a terminal state:

      live scrape   pending → COMPLETED / FAILED / SKIPPED
      uploaded HTML pending → HTML_UPLOADED → PARSED / FAILED

    HTML_UPLOADED means the page is stored in `raw_product_htmls` and waiting to
    be parsed; PARSED is its success state, kept distinct from COMPLETED so the
    admin UI can tell a hand-uploaded page from a live scrape. Both FAILED and
    HTML_UPLOADED are re-picked by the next bulk run.
    """

    PENDING = "pending"
    HTML_UPLOADED = "html_uploaded"
    PARSED = "parsed"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    ALL = (PENDING, HTML_UPLOADED, PARSED, COMPLETED, FAILED, SKIPPED)


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
    # See ScrapeStatus above for the full lifecycle
    scrape_status: str = Field(default=ScrapeStatus.PENDING)
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