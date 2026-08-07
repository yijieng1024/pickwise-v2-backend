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

from app.common.filter_service import apply_filters, apply_in, filter_query
from app.common.job_model import BackgroundJob, JobRead, JobStatus
from app.common.pagination_service import Page, PaginationParams, count_total, paginate
from app.database import get_session
from app.users.auth import get_current_admin

router = APIRouter(prefix="/jobs", tags=["Admin - Jobs"])

# Allow-list for apply_filters. Keys are the public filter names, which is why
# `status` appears here rather than the `status_filter` argument that carries it.
JOB_FILTERABLE_COLUMNS = {
    "job_type": BackgroundJob.job_type,
    "status": BackgroundJob.status,
}


@router.get("", response_model=Page[JobRead], dependencies=[Depends(get_current_admin)])
def list_jobs(
    job_type: Optional[str] = filter_query("e.g. processor.process_pending"),
    status_filter: Optional[str] = filter_query(
        "queued | processing | completed | failed", alias="status"
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

    statement = apply_filters(
        statement,
        {"job_type": job_type, "status": status_filter},
        JOB_FILTERABLE_COLUMNS,
    )
    # `active_only` is a shorthand for two statuses rather than a column of its
    # own, so it goes through apply_in instead of the field -> value map.
    if active_only:
        statement = apply_in(
            statement,
            BackgroundJob.status,  # type: ignore[arg-type]
            [JobStatus.QUEUED, JobStatus.PROCESSING],
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
