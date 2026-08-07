"""
job_model.py
------------
`background_jobs` — state for long-running admin operations that run outside
the request/response cycle.

Why a table and not an in-process dict: the admin UI polls this for minutes,
and Render restarts containers on every deploy. An in-memory registry would
lose every running job on restart (leaving the UI polling a 404 forever) and
would break outright if uvicorn were ever run with more than one worker, since
the poll could land on a process that never saw the job. A row survives both,
and lets `reset_stale_jobs()` mark orphaned runs as failed on startup instead
of leaving them stuck in `processing`.

The counters are deliberately generic (`processed`/`succeeded`/`failed`) so one
table and one polling endpoint serve every batch operation — scraping,
AI processing, tagging — rather than each growing its own status endpoint.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus:
    """Lifecycle: QUEUED → PROCESSING → COMPLETED | FAILED (both terminal)."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    TERMINAL = (COMPLETED, FAILED)
    ALL = (QUEUED, PROCESSING, COMPLETED, FAILED)


class JobType:
    """Stable identifiers the UI can branch on (icons, labels, links)."""

    PROCESS_PENDING = "processor.process_pending"
    CATEGORIZE_UNTAGGED = "processor.categorize_untagged"
    BULK_SCRAPE = "scraper.bulk_scrape"
    SCRAPE_TARGETS = "scraper.scrape_targets"
    GENERATE_EMBEDDINGS = "embeddings.generate_all"

    ALL = (
        PROCESS_PENDING,
        CATEGORIZE_UNTAGGED,
        BULK_SCRAPE,
        SCRAPE_TARGETS,
        GENERATE_EMBEDDINGS,
    )


class BackgroundJob(SQLModel, table=True):
    __tablename__ = "background_jobs"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    job_type: str = Field(index=True)
    status: str = Field(default=JobStatus.QUEUED, index=True)

    # Who started it — so the UI can show "started by" and filter to my jobs.
    created_by: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)

    # What was requested (limit, brand_id, target_ids …) — echoed back so a
    # poller that arrives late still knows what this run was for.
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))

    total_count: int = Field(default=0)
    processed_count: int = Field(default=0)
    succeeded_count: int = Field(default=0)
    failed_count: int = Field(default=0)

    # [{item, error}] — capped at _MAX_STORED_ERRORS in job_service
    errors: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))

    # Whole-job crash (worker raised) as opposed to per-item failures
    error_message: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    # Free-form result payload from the worker (totals, remaining counts, …)
    result: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))

    estimated_seconds: Optional[int] = Field(default=None)

    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    started_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


class JobAccepted(SQLModel):
    """202 body — everything the UI needs to start polling immediately."""

    job_id: uuid.UUID
    job_type: str
    status: str
    total_count: int
    estimated_seconds: Optional[int] = None
    estimated_completion_at: Optional[datetime] = None
    poll_url: str
    message: str


class JobRead(SQLModel):
    """Polling payload. `progress_percentage` is derived, never stored."""

    id: uuid.UUID
    job_type: str
    status: str
    created_by: Optional[uuid.UUID] = None
    params: dict[str, Any] = {}

    total_count: int
    processed_count: int
    succeeded_count: int
    failed_count: int
    progress_percentage: float

    errors: list[dict[str, Any]] = []
    error_message: Optional[str] = None
    result: Optional[dict[str, Any]] = None

    estimated_seconds: Optional[int] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @classmethod
    def from_job(cls, job: BackgroundJob) -> "JobRead":
        # A finished job always reads 100% — a worker that stopped early (its
        # source queue drained) must not leave the bar stuck at 60%.
        if job.status in JobStatus.TERMINAL:
            pct = 100.0
        elif job.total_count > 0:
            pct = round(min(job.processed_count / job.total_count, 1.0) * 100, 1)
        else:
            pct = 0.0

        return cls(
            id=job.id,
            job_type=job.job_type,
            status=job.status,
            created_by=job.created_by,
            params=job.params or {},
            total_count=job.total_count,
            processed_count=job.processed_count,
            succeeded_count=job.succeeded_count,
            failed_count=job.failed_count,
            progress_percentage=pct,
            errors=job.errors or [],
            error_message=job.error_message,
            result=job.result,
            estimated_seconds=job.estimated_seconds,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
