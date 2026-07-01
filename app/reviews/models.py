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
    match_confidence: Optional[float]
    status: str
    created_at: datetime


class ManualMatchRequest(SQLModel):
    laptop_id: uuid.UUID
