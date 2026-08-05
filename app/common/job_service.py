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
  background task must never touch it. Both sessions here are opened inside
  the task.
- Job bookkeeping gets its **own** session, separate from the worker's. If the
  worker's session is poisoned by a failed flush, writing the failure to the
  job row still succeeds — with a shared session the error report would be the
  second casualty of the same exception.
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
from app.database import engine
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

    Every `advance()` commits, because the whole point is that a poller sees
    movement while the job runs. These operations are seconds-per-item (LLM
    calls, page scrapes), so one small UPDATE per item is not measurable.
    """

    def __init__(self, session: Session, job_id: UUID):
        self._session = session
        self._job_id = job_id

    def _job(self) -> Optional[BackgroundJob]:
        return self._session.get(BackgroundJob, self._job_id)

    def _save(self, job: BackgroundJob) -> None:
        self._session.add(job)
        self._session.commit()

    def set_total(self, total: int) -> None:
        """Correct the estimate once the worker knows the real item count."""
        job = self._job()
        if job is None:
            return
        job.total_count = total
        self._save(job)

    def advance(
        self,
        *,
        succeeded: bool = True,
        item: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        job = self._job()
        if job is None:
            return

        job.processed_count += 1
        if succeeded:
            job.succeeded_count += 1
        else:
            job.failed_count += 1
            if error and len(job.errors) < _MAX_STORED_ERRORS:
                # Reassign rather than mutate — SQLAlchemy does not track
                # in-place edits of a JSONB list.
                job.errors = [*job.errors, {"item": item or "unknown", "error": error}]

        self._save(job)

    def mark_processing(self) -> None:
        job = self._job()
        if job is None:
            return
        job.status = JobStatus.PROCESSING
        job.started_at = _utcnow()
        self._save(job)

    def mark_completed(self, result: Optional[dict[str, Any]] = None) -> None:
        job = self._job()
        if job is None:
            return
        job.status = JobStatus.COMPLETED
        job.finished_at = _utcnow()
        if result is not None:
            job.result = result
        self._save(job)

    def mark_failed(self, message: str) -> None:
        job = self._job()
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.finished_at = _utcnow()
        job.error_message = message[:2000]
        self._save(job)


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
    with Session(engine) as job_session:
        progress = JobProgress(job_session, job_id)
        progress.mark_processing()

        try:
            with Session(engine) as work_session:
                if inspect.iscoroutinefunction(worker):
                    result = asyncio.run(worker(work_session, progress))
                else:
                    result = worker(work_session, progress)

            progress.mark_completed(result if isinstance(result, dict) else None)
            logger.info("job %s (%s) completed", job_id, _job_type(job_session, job_id))

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
                job = job_session.get(BackgroundJob, job_id)
                if job is not None and job.status not in JobStatus.TERMINAL:
                    job.status = JobStatus.FAILED
                    job.finished_at = _utcnow()
                    job.error_message = (
                        job.error_message
                        or "Job ended without reporting a terminal status."
                    )
                    job_session.add(job)
                    job_session.commit()
            except Exception:
                logger.exception("could not finalise job %s", job_id)


def _job_type(session: Session, job_id: UUID) -> str:
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
