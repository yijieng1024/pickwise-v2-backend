"""
`review_laptop_link`: which products a review actually covers (ADR-0012).

`raw_youtube_reviews.matched_laptop_id` is a single nullable FK, which encodes
two assumptions that are both false. First, that a review covers exactly one
laptop — 34.2% of tied videos span several families, and comparison videos
("ROG Zephyrus G16 vs TUF Gaming A18 vs ASUS V16") are common enough that one
of them holds the widest tie in the corpus at 23 candidates. Second, that the
thing a review covers is a *configuration*, when a reviewer covers a product
line and rarely states which RAM/SSD tier is on the bench.

This table replaces both assumptions with the pairing that matters:

    family_id   NOT NULL -- a review always resolves to a product line
    laptop_id   NULL     -- it resolves to a configuration only with evidence

NULL laptop_id is the honest answer, not a gap to be filled later. A link that
names the family and admits the configuration is unknown is strictly more
truthful than one that guesses a config, and downstream code can tell the two
apart — which it cannot do today, because a guessed match and a certain one are
both just a UUID in matched_laptop_id.

`match_source` exists for the same reason. `PATCH /reviews/raw/{id}/match`
writes match_confidence = 100.0 for a human decision, making it indistinguish-
able from a perfect fuzzy score; 10 of the 18 matched rows are human matches
wearing that disguise. Human links carry match_source=HUMAN and
match_confidence=NULL — a human did not score anything, and recording a
fabricated 100.0 would put a measurement where a judgement belongs.

`matched_laptop_id` stays live on raw_youtube_reviews through the cutover;
dropping it is a later revision once every reader has moved.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import field_validator
from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


class MatchSource(str, Enum):
    """Who decided this link.

    The distinction is not bookkeeping: an auto link is a hypothesis carrying a
    score and a tie width, and a human link is a decision carrying neither.
    Anything that weighs review evidence needs to tell them apart, and anything
    that re-runs matching must never overwrite a human link.
    """
    AUTO = "auto"
    HUMAN = "human"


MATCH_SOURCE_VALUES = {s.value for s in MatchSource}


class ReviewLaptopLink(SQLModel, table=True):
    __tablename__ = "review_laptop_link"  # type: ignore
    __table_args__ = (
        # One review may link a family once with an unknown config and once
        # with a known one; it may not link the same (family, config) twice.
        # NOTE: Postgres treats NULLs as distinct in a unique index, so this
        # does NOT prevent two rows with the same family and a NULL laptop_id.
        # Enforcing that needs a partial unique index; the create endpoint
        # checks for it explicitly instead, which also lets it return a usable
        # 409 rather than an IntegrityError.
        UniqueConstraint(
            "raw_review_id", "family_id", "laptop_id",
            name="uq_review_family_laptop",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    raw_review_id: uuid.UUID = Field(
        foreign_key="raw_youtube_reviews.id", index=True, nullable=False
    )
    family_id: uuid.UUID = Field(
        foreign_key="laptop_family.id", index=True, nullable=False
    )
    # NULL = the review covers this product line but the tested configuration
    # is unknown. See the module docstring.
    laptop_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="laptops.id", nullable=True
    )
    match_source: str = Field(default=MatchSource.AUTO.value, index=True)
    # NULL for human links — a human did not score anything.
    match_confidence: Optional[float] = Field(default=None, nullable=True)
    # How many candidates tied at rank 1 when an auto link was made. Nullable
    # because a human link has no tie, and because rows backfilled from
    # matched_laptop_id predate the measurement.
    tie_width: Optional[int] = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# --- API schemas ---

class ReviewLinkCreate(SQLModel):
    family_id: uuid.UUID
    laptop_id: Optional[uuid.UUID] = None

    @field_validator("family_id")
    @classmethod
    def family_required(cls, value: uuid.UUID) -> uuid.UUID:
        # Belt and braces: the type already forbids None, but the pairing is
        # the point of the table and a future schema edit relaxing it should
        # trip here rather than silently produce family-less links.
        if value is None:
            raise ValueError("family_id is required — a review always resolves "
                             "to a product line, even when the config is unknown")
        return value


class ReviewLinkRead(SQLModel):
    id: uuid.UUID
    raw_review_id: uuid.UUID
    family_id: uuid.UUID
    family_name: Optional[str] = None
    laptop_id: Optional[uuid.UUID] = None
    # None both when laptop_id is NULL and when the laptop has been deleted;
    # the caller distinguishes by looking at laptop_id itself.
    laptop_name: Optional[str] = None
    match_source: str
    match_confidence: Optional[float] = None
    tie_width: Optional[int] = None
    created_at: datetime
