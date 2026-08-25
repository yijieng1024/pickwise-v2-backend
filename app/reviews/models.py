import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pgvector.sqlalchemy import Vector
from pydantic import field_validator, model_validator
from sqlalchemy import Column, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ReviewStatus(str, Enum):
    """Triage state of an ingested video.

    Why a transcript fetch produced nothing lives in `failure_reason` (a
    TranscriptFailure value), not here — REJECTED means "no transcript", and
    splitting it by cause would make every consumer enumerate causes to ask
    that one question.

    IRRELEVANT is NOT a flavour of REJECTED, and the two must not be merged.
    REJECTED says the video is about a laptop but we could not get its words;
    IRRELEVANT says the video is not about a laptop at all. They differ in
    everything that acts on them: a rejected row is a transcript-retry
    candidate (see POST /reviews/retry-transcripts), an irrelevant one never
    is, no matter how many transcripts become available. Collapsing them would
    recreate exactly the cause ambiguity the failure_reason split removed.

    IRRELEVANT is a dismissal, not a deletion, and the row is what makes the
    dismissal stick: video_id is UNIQUE and ingest_for_laptop skips any
    existing row that is not REJECTED, so a deleted row is rediscovered and
    reinserted on the next run and lands straight back in the queue. Same
    reasoning as keeping the terminal no_track rows — the row is how ingest
    knows not to try again.
    """
    PENDING = "pending"        # transcript stored, no confident laptop match
    MATCHED = "matched"        # linked to a laptop, ready for processing
    REJECTED = "rejected"      # no transcript
    IRRELEVANT = "irrelevant"  # not a laptop video — dismissed by a human


class TrustTier(str, Enum):
    """DEPRECATED — superseded by EvidenceTier / MarketRelevance /
    ReviewLanguage below. The column is still written by the channel API and
    read by nothing; it is kept live through the cutover and dropped in a
    later revision. New code must not read it."""
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


# `trust_tier` conflated three independent properties of a channel, and they do
# not move together: a Taiwanese channel can be excellent on evidence quality
# and useless on Malaysian pricing. One field could only ever express one of
# them, so ordering or filtering on it silently mixed the three.
class EvidenceTier(str, Enum):
    """Does the reviewer actually test the machine — benchmarks, thermals,
    sustained load — or relay spec sheets and first impressions?

    This is the only one of the three that speaks to whether a claim is well
    evidenced, so it is the one the aggregator orders on.
    """
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


class MarketRelevance(str, Enum):
    """Whose prices and availability does this channel quote?

    REGIONAL is deliberately vague about *which* region. Singapore and Taiwan
    are both non-Malaysian and non-global but are not equivalent to our users —
    an SGD price sits against retail channels that overlap ours, a TWD price
    does not — so collapsing them into one "SEA" bucket would discard the only
    part of the signal we would act on. A finer value set would be designing
    for data we do not have: every channel on the roster is GLOBAL today.
    Tighten this when the roster justifies it.
    """
    MY = "my"              # quotes RM against Malaysian retail
    REGIONAL = "regional"  # neighbouring market: SGD, TWD, THB...
    GLOBAL = "global"      # USD / no local pricing


class ReviewLanguage(str, Enum):
    """What language the channel reviews in. Seeded from the script of the
    titles we have already ingested — see app/scripts/seed_channel_fields.py.

    MIXED means the channel publishes content in BOTH languages — separate
    videos in each, which is how Mint earned it (genuinely separate zh and en
    titles). It does NOT mean one language carrying loanwords from another. A
    Chinese-language review that says "Zenbook Duo" and "RTX 5060" in Latin
    script is ZH, not MIXED: embedded English product names are universal in
    Chinese tech media and carry no signal about what language the review is
    in. Zing Gadget is the worked example — 50 of 50 Chinese titles, every one
    of them containing Latin-script model names, classified ZH.

    Why the distinction matters rather than being pedantry: this field exists
    to decide which corpus a query can draw on, and a Chinese review with
    English product names is fully usable for a Chinese-language query.
    Calling it MIXED would misdescribe it and would imply an English corpus
    that does not exist.

    A note on the value set itself: no Malaysian channel on the roster titles
    in Malay — the detector was run over 350 titles across all 8 channels and
    fired zero times (2026-08-24). Every Malaysian channel titles in English
    or Chinese. That bounds what this field can currently express, and it was
    checked rather than assumed, so do not read the absence of a `ms` member
    as an oversight.
    """
    EN = "en"
    ZH = "zh"
    MIXED = "mixed"


REVIEW_STATUS_VALUES = {s.value for s in ReviewStatus}
TRUST_TIER_VALUES = {t.value for t in TrustTier}
EVIDENCE_TIER_VALUES = {t.value for t in EvidenceTier}
MARKET_RELEVANCE_VALUES = {m.value for m in MarketRelevance}
REVIEW_LANGUAGE_VALUES = {l.value for l in ReviewLanguage}


def _validate_choice(value: Optional[str], allowed: set[str], field: str) -> Optional[str]:
    """Both columns stay plain VARCHAR rather than native Postgres enums, the
    same call laptop_models.py makes for LaptopStatus: adding a state later
    then needs no ALTER TYPE migration, and validation lives at the API
    boundary instead.

    The catch, also inherited: SQLModel skips validation on `table=True`
    models, so this only binds writes that come in through the API schemas
    below. A direct `channel.trust_tier = "banana"` in a script still gets
    through — which is why the enum members, not string literals, are what
    application code should compare against.
    """
    if value is None:
        return value
    if value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")
    return value


class YoutubeChannel(SQLModel, table=True):
    __tablename__ = "youtube_channels"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    channel_id: str = Field(unique=True, index=True)  # YouTube UCxxxxxx ID
    channel_name: str
    channel_img_url: Optional[str] = Field(default=None, nullable=True)
    # Deprecated, kept live through the cutover. See TrustTier.
    trust_tier: str = Field(default=TrustTier.TIER_2.value)
    # server_default on all three, not just a Python-side default: SQLModel's
    # `default` is applied at insert time, so autogenerate would emit a bare
    # `ADD COLUMN ... NOT NULL`, which Postgres refuses on a populated table.
    # The server default is also what keeps the model matching the live column
    # for `alembic check`. Same reasoning as transcript_attempts.
    evidence_tier: str = Field(
        default=EvidenceTier.TIER_2.value,
        sa_column_kwargs={"server_default": EvidenceTier.TIER_2.value},
    )
    market_relevance: str = Field(
        default=MarketRelevance.GLOBAL.value,
        sa_column_kwargs={"server_default": MarketRelevance.GLOBAL.value},
    )
    review_language: str = Field(
        default=ReviewLanguage.EN.value,
        sa_column_kwargs={"server_default": ReviewLanguage.EN.value},
    )
    # When a human last judged evidence_tier. NULL means genuinely unreviewed.
    #
    # This column exists because "confirmed tier_2" and "nobody looked" are the
    # same byte in evidence_tier, and the aggregator's ORDER BY depends on that
    # byte — so without this, ranking cannot tell a judgement from a default.
    #
    # Nullable with NO server_default, and that is the whole design. A server
    # default would backfill every pre-existing row with a timestamp, making
    # unreviewed channels indistinguishable from reviewed ones — precisely the
    # failure this column exists to prevent, and the same trap documented at
    # the retry-transcripts predicate in router.py, where transcript_attempts'
    # server_default '0' made 28 rows with full transcripts read as never
    # attempted. A default value is a fact about the migration, not about the
    # row.
    evidence_tier_reviewed_at: Optional[datetime] = Field(
        default=None, nullable=True
    )
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RawYoutubeReview(SQLModel, table=True):
    __tablename__ = "raw_youtube_reviews"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    video_id: str = Field(unique=True, index=True)
    channel_id: str = Field(foreign_key="youtube_channels.channel_id", index=True)
    video_title: str
    # The video's full description text. Nullable, and NULL genuinely means
    # "never fetched" — every row ingested before this column existed, plus any
    # row whose videos.list lookup failed. It is not "the video has no
    # description"; an empty description is stored as "".
    #
    # Worth the extra API call: it is the single richest evidence source for
    # which configuration a reviewer tested. Titles never carry a spec, and
    # Chinese-language channels routinely paste a full spec table into the
    # description. search.list only returns a truncated description (~160
    # chars, cutting off exactly where the spec table starts), so this is
    # filled from videos.list — 1 quota unit per 50 videos against search's 100
    # per channel, i.e. rounding error on the ingest budget.
    video_description: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    published_at: Optional[datetime] = None
    raw_transcript: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    matched_laptop_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="laptops.id", nullable=True
    )
    match_confidence: Optional[float] = None
    status: str = Field(default=ReviewStatus.PENDING.value)
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
    trust_tier: str = TrustTier.TIER_2.value  # deprecated, see TrustTier
    evidence_tier: str = EvidenceTier.TIER_2.value
    market_relevance: str = MarketRelevance.GLOBAL.value
    review_language: str = ReviewLanguage.EN.value
    active: bool = True

    @field_validator("trust_tier")
    @classmethod
    def check_trust_tier(cls, value: str) -> str:
        return _validate_choice(value, TRUST_TIER_VALUES, "trust_tier")  # type: ignore[return-value]

    @field_validator("evidence_tier")
    @classmethod
    def check_evidence_tier(cls, value: str) -> str:
        return _validate_choice(value, EVIDENCE_TIER_VALUES, "evidence_tier")  # type: ignore[return-value]

    @field_validator("market_relevance")
    @classmethod
    def check_market_relevance(cls, value: str) -> str:
        return _validate_choice(value, MARKET_RELEVANCE_VALUES, "market_relevance")  # type: ignore[return-value]

    @field_validator("review_language")
    @classmethod
    def check_review_language(cls, value: str) -> str:
        return _validate_choice(value, REVIEW_LANGUAGE_VALUES, "review_language")  # type: ignore[return-value]


class YoutubeChannelUpdate(SQLModel):
    channel_name: Optional[str] = None
    channel_img_url: Optional[str] = None
    trust_tier: Optional[str] = None  # deprecated, see TrustTier
    evidence_tier: Optional[str] = None
    market_relevance: Optional[str] = None
    review_language: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("trust_tier")
    @classmethod
    def check_trust_tier(cls, value: Optional[str]) -> Optional[str]:
        return _validate_choice(value, TRUST_TIER_VALUES, "trust_tier")

    @field_validator("evidence_tier")
    @classmethod
    def check_evidence_tier(cls, value: Optional[str]) -> Optional[str]:
        return _validate_choice(value, EVIDENCE_TIER_VALUES, "evidence_tier")

    @field_validator("market_relevance")
    @classmethod
    def check_market_relevance(cls, value: Optional[str]) -> Optional[str]:
        return _validate_choice(value, MARKET_RELEVANCE_VALUES, "market_relevance")

    @field_validator("review_language")
    @classmethod
    def check_review_language(cls, value: Optional[str]) -> Optional[str]:
        return _validate_choice(value, REVIEW_LANGUAGE_VALUES, "review_language")


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
    # Passed through raw, deliberately NOT validated against ReviewStatus.
    # A validator's job is to keep bad values out of the database, not to keep
    # us from seeing values already in it. Validating here inverts that: an
    # unexpected status would break GET /reviews/raw entirely, taking out the
    # admin queue at exactly the moment it is needed to diagnose the problem.
    # It is also fragile against planned work — ADR-0013 adds a no_signal
    # route and ADR-0012 changes how matches are stored, and a new value
    # reaching the table before this enum is updated would turn a benign
    # schema lag into a broken page. Writes are guarded instead: nothing in
    # this package assigns a status literal, only ReviewStatus members.
    status: str
    created_at: datetime


class ReviewIrrelevantRequest(SQLModel):
    """Body for PATCH /reviews/raw/{id}/irrelevant.

    One endpoint with a flag rather than a mark endpoint and an undo endpoint:
    the undo has to be as discoverable as the dismissal, and a second route
    named /un-irrelevant is the kind of thing that gets built and then never
    wired into the screen. `irrelevant: false` restores the row to PENDING.

    Defaults to True so the dismiss call can send an empty body, but the
    restore call must say so explicitly — the destructive-ish direction is the
    common one, the reversal should be deliberate.
    """
    irrelevant: bool = True


class ManualMatchRequest(SQLModel):
    """Body for PATCH /reviews/raw/{id}/match.

    Same shape as ReviewLinkCreate (family_id + optional laptop_id), so one
    body works on both write paths and there is no second convention to learn.

    Both fields are optional here, unlike ReviewLinkCreate, for one reason:
    this endpoint already exists and its callers send `{laptop_id}` alone.
    Making family_id required would break them for no gain, so an omitted
    family_id is resolved from the laptop instead. At least one must be
    present; family_id alone is the "tested configuration unknown" case.
    """
    family_id: Optional[uuid.UUID] = None
    laptop_id: Optional[uuid.UUID] = None

    @model_validator(mode="after")
    def one_of(self) -> "ManualMatchRequest":
        if self.family_id is None and self.laptop_id is None:
            raise ValueError(
                "Provide family_id, laptop_id, or both. family_id alone links "
                "the review to a product line with the configuration unknown."
            )
        return self
