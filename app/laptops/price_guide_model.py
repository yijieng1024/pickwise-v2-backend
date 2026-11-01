"""
`price_guides` / `price_guide_rows`: what one ASUS product guide PDF said.

A staging area. The GitHub Actions workflow writes here and nowhere else; a price
reaches `laptops` only through the admin apply (app/laptops/price_guide_service.py).
The PDF itself lives in Drive, and `pdf_sha256` identifies the exact file a price
came from.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


class PriceGuideKind(str, Enum):
    CONSUMER = "consumer"
    ROG = "rog"
    BUSINESS = "business"


class PriceGuide(SQLModel, table=True):
    __tablename__ = "price_guides"  # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    kind: str = Field(max_length=20, index=True)
    filename: str
    pdf_sha256: str = Field(max_length=64, unique=True)
    edition_text: Optional[str] = None
    # The guide's date: the PDFs carry no date of their own, only an edition.
    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    # Null when the workflow was started by hand from the Actions tab.
    uploaded_by: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id")
    row_count: int


class PriceGuideRow(SQLModel, table=True):
    __tablename__ = "price_guide_rows"  # type: ignore
    __table_args__ = (UniqueConstraint("guide_id", "sku", name="uq_price_guide_rows_guide_sku"),)
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    guide_id: uuid.UUID = Field(foreign_key="price_guides.id", index=True)
    sku: str
    model_code: str  # the part of the SKU before the hyphen
    cpu: Optional[str] = None
    ram_gb: Optional[int] = None
    ssd_gb: Optional[int] = None
    price_rm: Optional[float] = None
    page: int
