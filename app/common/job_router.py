"""
job_router.py
-------------
Polling endpoints for background jobs.

Deliberately mounted at `/jobs`, not `/scraper/jobs`: jobs are started by the
scraper *and* the processor (and anything batch-shaped added later), so a
brand- or module-scoped path would be wrong for half of them. One poller in the
admin UI handles every batch operation.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.common.job_model import BackgroundJob, JobRead, JobStatus
from app.common.pagination_service import Page, PaginationParams, count_total, paginate
from app.database import get_session
from app.users.auth import get_current_admin

router = APIRouter(prefix="/jobs", tags=["Admin - Jobs"])


@router.get("", response_model=Page[JobRead], dependencies=[Depends(get_current_admin)])
def list_jobs(
    job_type: Optional[str] = Query(default=None, description="e.g. processor.process_pending"),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description="queued | processing | completed | failed",
    ),
    active_only: bool = Query(
        default=False, description="Only jobs still queued or processing"
    ),
    pagination: PaginationParams = Depends(),
    session: Session = Depends(get_session),
):
    """
    Job history, newest first.

    `active_only=true` is what a global "something is running" indicator should
    poll — it answers the question without fetching the whole history.
    """
    statement = select(BackgroundJob)

    if job_type:
        statement = statement.where(BackgroundJob.job_type == job_type)
    if status_filter:
        statement = statement.where(BackgroundJob.status == status_filter)
    if active_only:
        statement = statement.where(
            BackgroundJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING])  # type: ignore[attr-defined]
        )

    total = count_total(session, statement)

    statement = statement.order_by(BackgroundJob.created_at.desc())  # type: ignore[attr-defined]
    jobs = session.exec(paginate(statement, pagination)).all()

    return Page(
        items=[JobRead.from_job(j) for j in jobs],
        total=total,
        skip=pagination.skip,
        limit=pagination.limit,
    )


@router.get("/{job_id}", response_model=JobRead, dependencies=[Depends(get_current_admin)])
def get_job(job_id: UUID, session: Session = Depends(get_session)):
    """
    Live progress for one job — poll this after a 202.

    Stop polling when `status` is `completed` or `failed`; both are terminal.
    `errors[]` carries per-item failures (each `{item, error}`) and is
    populated while the job is still running, so a partially-failing run is
    visible immediately rather than only at the end.
    """
    job = session.get(BackgroundJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found. Job history is kept in the database, so this id was never valid.",
        )
    return JobRead.from_job(job)
