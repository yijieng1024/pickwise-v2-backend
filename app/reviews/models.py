import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class YoutubeChannel(SQLModel, table=True):
    __tablename__ = "youtube_channels"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    channel_id: str = Field(unique=True, index=True)  # YouTube UCxxxxxx ID
    channel_name: str
    channel_img_url: Optional[str] = Field(default=None, nullable=True)
    trust_tier: str = Field(default="tier_2")  # tier_1 or tier_2
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RawYoutubeReview(SQLModel, table=True):
    __tablename__ = "raw_youtube_reviews"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    video_id: str = Field(unique=True, index=True)
    channel_id: str = Field(foreign_key="youtube_channels.channel_id", index=True)
    video_title: str
    published_at: Optional[datetime] = None
    raw_transcript: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    matched_laptop_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="laptops.id", nullable=True
    )
    match_confidence: Optional[float] = None
    status: str = Field(default="pending")  # pending | matched | rejected
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Why the fetch produced no transcript. NULL means "never recorded" —
    # every row ingested before this field existed. Those are treated as
    # retryable once, because the old code erased the reason.
    failure_reason: Optional[str] = Field(default=None, nullable=True, index=True)
    # Source language of the stored transcript, e.g. 'en-US', 'zh-Hans'.
    # The processor needs this to decide paraphrase language.
    transcript_language: Optional[str] = Field(default=None, nullable=True)
    # server_default, not just default=0: SQLModel's `default` is applied in
    # Python at insert time, so autogenerate would emit a bare
    # `ADD COLUMN ... NOT NULL` — which Postgres refuses on a table that
    # already has rows. The server default also keeps the model matching the
    # live column, which is what `alembic check` compares against.
    transcript_attempts: int = Field(
        default=0, sa_column_kwargs={"server_default": "0"}
    )


class LaptopReviewChunk(SQLModel, table=True):
    __tablename__ = "laptop_review_chunks"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    video_id: str = Field(index=True)
    channel_name: str
    timestamp_start_seconds: int
    timestamp_end_seconds: int
    chunk_text: str  # LLM-paraphrased summary — never verbatim transcript
    embedding: Any = Field(sa_column=Column(Vector(768)))
    sentiment_tag: str  # strength | weakness | neutral
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LaptopReviewSummary(SQLModel, table=True):
    __tablename__ = "laptop_review_summary"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", unique=True, index=True)
    aggregated_strengths: List[str] = Field(
        default_factory=list, sa_column=Column(JSONB)
    )
    aggregated_weaknesses: List[str] = Field(
        default_factory=list, sa_column=Column(JSONB)
    )
    review_count: int = Field(default=0)
    last_updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

# --- Read schemas ---

class YoutubeChannelCreate(SQLModel):
    channel_url: str  # YouTube URL, @handle, or UC... ID — resolved automatically
    trust_tier: str = "tier_2"
    active: bool = True


class YoutubeChannelUpdate(SQLModel):
    channel_name: Optional[str] = None
    channel_img_url: Optional[str] = None
    trust_tier: Optional[str] = None
    active: Optional[bool] = None


class RawYoutubeReviewRead(SQLModel):
    id: uuid.UUID
    video_id: str
    channel_id: str
    video_title: str
    published_at: Optional[datetime]
    matched_laptop_id: Optional[uuid.UUID]
    # Resolved from `matched_laptop_id` by the listing route, not stored on the
    # table. Without it every client showing a match has to fetch the whole
    # laptop catalog just to turn one id into a name. None when unmatched, or
    # when the laptop has since been deleted.
    matched_laptop_name: Optional[str] = None
    match_confidence: Optional[float]
    status: str
    created_at: datetime


class ManualMatchRequest(SQLModel):
    laptop_id: uuid.UUID
