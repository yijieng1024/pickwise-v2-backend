"""
raw_html_model.py
-----------------
`raw_product_htmls` — stored source HTML for pages that cannot be fetched
automatically and are uploaded by an admin instead (today: Acer, whose store
refuses automated requests; see `acer_scraper.py`).

Deliberately **product-agnostic**, so monitors/desktops/phones can reuse it
without a schema change:

- `product_type_id` scopes a row to a product line (`product_types.name`, e.g.
  "laptop"), which is what decides *which* target table `target_id` points at.
- `target_id` is a bare indexed UUID with **no foreign key**. The target table
  is per product type (`laptop_scrape_urls` today, `monitor_scrape_urls`
  tomorrow), and a real FK can only point at one table. Integrity is enforced
  at the service layer (`html_ingest.py`), which never writes a row without
  first resolving a live target.
- `brand_id` does keep its FK — brands are already shared across the app via
  the single `laptop_brands` table.

`canonical_url` is unique: it comes from the page's own
`<link rel="canonical">`, so it identifies the product globally and is the
natural upsert key. Re-uploading a page overwrites `raw_html` and bumps
`updated_at`, which is how a stale price gets refreshed.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Text
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RawProductHtml(SQLModel, table=True):
    __tablename__ = "raw_product_htmls"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    product_type_id: uuid.UUID = Field(foreign_key="product_types.id", index=True)
    brand_id: uuid.UUID = Field(foreign_key="laptop_brands.id", index=True)

    # Row id in the product type's own target table (laptop_scrape_urls.id for
    # product_type "laptop"). Not an FK — see the module docstring.
    target_id: uuid.UUID = Field(index=True)

    canonical_url: str = Field(unique=True, index=True)

    raw_html: str = Field(sa_column=Column(Text, nullable=False))

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


class RawHtmlDocument(SQLModel):
    """One page in a JSON upload payload."""

    raw_html: str
    # Optional override for pages whose canonical tag is missing or wrong.
    canonical_url: Optional[str] = None
    # Free-text label echoed back in the per-item result (e.g. a filename)
    source_name: Optional[str] = None


class RawHtmlUploadRequest(SQLModel):
    documents: list[RawHtmlDocument]


class RawHtmlItemResult(SQLModel):
    source_name: str
    status: str  # "matched" | "unmatched" | "invalid"
    canonical_url: Optional[str] = None
    target_id: Optional[uuid.UUID] = None
    created: Optional[bool] = None  # True = inserted, False = updated
    error: Optional[str] = None
    # Stored fine, but something about the page looks wrong — most often a
    # "Webpage, Complete" save, which rewrites image URLs to local paths that
    # are never uploaded. Distinct from `error`: the page was still accepted.
    warning: Optional[str] = None


class RawHtmlUploadSummary(SQLModel):
    received: int
    matched: int
    unmatched: int
    invalid: int
    inserted: int
    updated: int
    # Subset of `matched` that stored cleanly but carried a warning.
    warnings: int = 0
    results: list[RawHtmlItemResult]
