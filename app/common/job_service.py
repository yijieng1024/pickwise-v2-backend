"""
job_service.py
--------------
Runs long batch operations outside the request/response cycle and records their
progress in `background_jobs`.

Contract for callers:

    job = create_job(session, job_type=..., total_count=n, seconds_per_item=5)
    background_tasks.add_task(run_job, job.id, worker)
    return job_accepted(job)          # 202

`worker(work_session, progress)` does the actual work and calls
`progress.advance(...)` per item.

Session lifecycle — the reason this module exists rather than each router
doing it inline:

- The request's session is **closed** once the response is sent, so a
  background task must never touch it. Every session here is opened inside
  the task.
- Job bookkeeping never shares the worker's session. If the worker's session is
  poisoned by a failed flush, writing the failure to the job row still succeeds
  — with a shared session the error report would be the second casualty of the
  same exception.
- Bookkeeping opens a **new short session per update** rather than holding one
  for the length of the job. A job runs for minutes to hours, and a bookkeeping
  connection held that whole time is idle for essentially all of it while
  counting against the pool (that, doubled per concurrent job, is what used to
  exhaust it). A running job now occupies one pooled connection — the worker's
  — plus a checkout of a few milliseconds per `advance()`.
- `finally` guarantees a terminal status. A job can only stay `processing`
  if the process itself dies, and `reset_stale_jobs()` cleans those up on the
  next startup.
"""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.common.job_model import BackgroundJob, JobAccepted, JobStatus
from app.database import engine, session_scope
from app.logger import get_logger

logger = get_logger(__name__)

# Enough to diagnose a bad run without letting one pathological job write a
# multi-megabyte JSONB blob.
_MAX_STORED_ERRORS = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Progress handle — what workers call
# ---------------------------------------------------------------------------


class JobProgress:
    """
    Live counters for one running job.

    Every update commits, because the whole point is that a poller sees
    movement while the job runs. Each one also opens and closes its own
    session: these operations are seconds-per-item (LLM calls, page scrapes),
    so neither the extra UPDATE nor the pool checkout around it is measurable,
    and in exchange the job holds no idle bookkeeping connection between items.
    """

    def __init__(self, job_id: UUID):
        self._job_id = job_id

    def _update(self, mutate: Callable[[BackgroundJob], None]) -> None:
        """Apply *mutate* to the job row and commit, in a session of its own."""
        with session_scope() as session:
            job = session.get(BackgroundJob, self._job_id)
            if job is None:
                return
            mutate(job)
            session.add(job)
            session.commit()

    def set_total(self, total: int) -> None:
        """Correct the estimate once the worker knows the real item count."""

        def _apply(job: BackgroundJob) -> None:
            job.total_count = total

        self._update(_apply)

    def advance(
        self,
        *,
        succeeded: bool = True,
        item: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        def _apply(job: BackgroundJob) -> None:
            job.processed_count += 1
            if succeeded:
                job.succeeded_count += 1
            else:
                job.failed_count += 1
                if error and len(job.errors) < _MAX_STORED_ERRORS:
                    # Reassign rather than mutate — SQLAlchemy does not track
                    # in-place edits of a JSONB list.
                    job.errors = [
                        *job.errors,
                        {"item": item or "unknown", "error": error},
                    ]

        self._update(_apply)

    def mark_processing(self) -> None:
        def _apply(job: BackgroundJob) -> None:
            job.status = JobStatus.PROCESSING
            job.started_at = _utcnow()

        self._update(_apply)

    def mark_completed(self, result: Optional[dict[str, Any]] = None) -> None:
        def _apply(job: BackgroundJob) -> None:
            job.status = JobStatus.COMPLETED
            job.finished_at = _utcnow()
            if result is not None:
                job.result = result

        self._update(_apply)

    def mark_failed(self, message: str) -> None:
        def _apply(job: BackgroundJob) -> None:
            job.status = JobStatus.FAILED
            job.finished_at = _utcnow()
            job.error_message = message[:2000]

        self._update(_apply)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_job(
    session: Session,
    *,
    job_type: str,
    total_count: int,
    params: Optional[dict[str, Any]] = None,
    created_by: Optional[UUID] = None,
    seconds_per_item: Optional[float] = None,
    overhead_seconds: int = 5,
) -> BackgroundJob:
    """Insert a `queued` job row. Commits so the background task can find it."""
    estimated = None
    if seconds_per_item is not None and total_count > 0:
        estimated = int(total_count * seconds_per_item + overhead_seconds)

    job = BackgroundJob(
        job_type=job_type,
        status=JobStatus.QUEUED,
        created_by=created_by,
        params=params or {},
        total_count=total_count,
        estimated_seconds=estimated,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def job_accepted(job: BackgroundJob, message: Optional[str] = None) -> JobAccepted:
    """Build the 202 body, including where to poll."""
    eta = None
    if job.estimated_seconds:
        eta = job.created_at + timedelta(seconds=job.estimated_seconds)

    return JobAccepted(
        job_id=job.id,
        job_type=job.job_type,
        status=job.status,
        total_count=job.total_count,
        estimated_seconds=job.estimated_seconds,
        estimated_completion_at=eta,
        poll_url=f"/api/v2/jobs/{job.id}",
        message=message
        or (
            f"Accepted. {job.total_count} item(s) queued — poll the job for progress."
        ),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_job(job_id: UUID, worker: Callable[[Session, JobProgress], Any]) -> None:
    """
    Execute *worker* under job bookkeeping. Handed to `BackgroundTasks`.

    Sync callable on purpose: FastAPI runs sync background tasks in a worker
    thread, which keeps these long blocking loops (LLM throttles, Playwright)
    off the event loop. An async worker is run via `asyncio.run` in that same
    thread — safe because the scrapers already offload Playwright to their own
    loop (see scraper/playwright_utils.py).
    """
    progress = JobProgress(job_id)
    progress.mark_processing()

    try:
        # The worker's session is the only connection this job holds for its
        # full duration, and it is released the moment the work is done —
        # before the completion bookkeeping below runs.
        with Session(engine) as work_session:
            if inspect.iscoroutinefunction(worker):
                result = asyncio.run(worker(work_session, progress))
            else:
                result = worker(work_session, progress)

        progress.mark_completed(result if isinstance(result, dict) else None)
        logger.info("job %s (%s) completed", job_id, _job_type(job_id))

    except Exception as e:
        logger.exception("job %s failed", job_id)
        try:
            progress.mark_failed(f"{type(e).__name__}: {e}")
        except Exception:
            # Bookkeeping itself failed — the finally below is the backstop.
            logger.exception("could not record failure for job %s", job_id)

    finally:
        # Backstop: no path may leave a job in a non-terminal state.
        try:
            with session_scope() as session:
                job = session.get(BackgroundJob, job_id)
                if job is not None and job.status not in JobStatus.TERMINAL:
                    job.status = JobStatus.FAILED
                    job.finished_at = _utcnow()
                    job.error_message = (
                        job.error_message
                        or "Job ended without reporting a terminal status."
                    )
                    session.add(job)
                    session.commit()
        except Exception:
            logger.exception("could not finalise job %s", job_id)


def _job_type(job_id: UUID) -> str:
    with session_scope() as session:
        job = session.get(BackgroundJob, job_id)
        return job.job_type if job else "?"


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


def reset_stale_jobs() -> int:
    """
    Fail any job left mid-flight by a previous process.

    Jobs run in-process, so a deploy or crash orphans anything still running.
    Called once at startup; without it those rows would report `processing`
    forever and the UI would poll a job that no longer exists.
    """
    try:
        with Session(engine) as session:
            stale = session.exec(
                select(BackgroundJob).where(
                    BackgroundJob.status.in_(  # type: ignore[attr-defined]
                        [JobStatus.QUEUED, JobStatus.PROCESSING]
                    )
                )
            ).all()

            for job in stale:
                job.status = JobStatus.FAILED
                job.finished_at = _utcnow()
                job.error_message = (
                    "Interrupted — the server restarted while this job was running. "
                    "Re-run it; completed items are not repeated."
                )
                session.add(job)

            if stale:
                session.commit()
                logger.warning("marked %d interrupted job(s) as failed on startup", len(stale))
            return len(stale)
    except Exception:
        # Never block startup on bookkeeping (e.g. migrations not yet applied).
        logger.exception("could not reset stale jobs on startup")
        return 0
