from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.common.job_model import JobAccepted, JobType
from app.common.job_service import JobProgress, create_job, job_accepted, run_job
from app.database import get_session
from app.laptops.laptop_category_model import LaptopCategory
from app.laptops.laptop_models import Laptop
from app.logger import get_logger
from app.common.rate_limit import seconds_per_call
from app.processor.engine import (
    _CATEGORIZE_TOKENS_PER_CALL,
    _EXTRACTION_TOKENS_PER_CALL,
    categorize_untagged_laptops,
    process_pending_laptops,
    process_raw_laptop_data,
)
from app.scraper.models import RawScrapLaptop
from app.users.auth import get_current_admin
from app.users.models import User

logger = get_logger(__name__)

router = APIRouter(prefix="/processor", tags=["Processor"])

# Gemma 4 31B free-tier limits (gemma-4-31b-it):
#  16K TPM  →  the binding limit; the engine's rate limiter paces against it
#  1500 RPD →  default batch of 100 per run; hard cap at 1500
#
# Derived from the same limiter the engine actually uses rather than restated,
# so the ETA in the 202 cannot drift away from the pacing it describes. It was
# a hardcoded 5, which under-reported the real runtime by ~2.6x once pacing
# moved onto the token budget.
_SECONDS_PER_ITEM      = seconds_per_call(_EXTRACTION_TOKENS_PER_CALL)
# Categorisation sends a much smaller prompt, so it is paced faster.
_SECONDS_PER_TAG_ITEM  = seconds_per_call(_CATEGORIZE_TOKENS_PER_CALL)
_DEFAULT_BATCH_LIMIT   = 100
_MAX_BATCH_LIMIT       = 1500


@router.post("/process/{raw_laptop_id}", dependencies=[Depends(get_current_admin)])
def process_single_laptop(
    raw_laptop_id: str,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """
    Process a single raw scraped laptop. Runs inline — one record is a single
    LLM call, fast enough to answer within the request.
    """
    result = process_raw_laptop_data(raw_laptop_id, session)

    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    return result


@router.post(
    "/process-pending",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def process_all_pending_laptops(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
    limit: int = Query(
        default=_DEFAULT_BATCH_LIMIT,
        ge=1,
        le=_MAX_BATCH_LIMIT,
        description=(
            f"Max records to process in this run (default {_DEFAULT_BATCH_LIMIT}). "
            f"Hard ceiling is {_MAX_BATCH_LIMIT} to stay within the Gemma 4 "
            "free-tier daily quota (1500 RPD)."
        ),
    ),
) -> JobAccepted:
    """
    Start processing pending scraped records through the AI extractor.

    **Returns 202 immediately — it does not wait for the work.** Each record
    costs one throttled LLM call, paced against the 16K TPM free-tier budget
    rather than a fixed delay, so a default batch of 100 runs for roughly 20
    minutes; holding the connection open for that long risks a proxy or
    network timeout, and offered no progress feedback.

    Poll `GET /api/v2/jobs/{job_id}` (returned as `poll_url`) for live counts,
    per-item errors and a percentage. Stop polling when `status` is
    `completed` or `failed`.
    """
    queued = len(
        session.exec(
            select(RawScrapLaptop.id)
            .where(RawScrapLaptop.processing_status == "pending")
            .limit(limit)
        ).all()
    )

    job = create_job(
        session,
        job_type=JobType.PROCESS_PENDING,
        total_count=queued,
        params={"limit": limit},
        created_by=current_admin.id,
        seconds_per_item=_SECONDS_PER_ITEM,
    )

    def worker(work_session: Session, progress: JobProgress) -> dict:
        return process_pending_laptops(work_session, limit=limit, progress=progress)

    background_tasks.add_task(run_job, job.id, worker)

    return job_accepted(
        job,
        message=(
            f"Processing {queued} pending record(s) in the background."
            if queued
            else "No pending records found — the job will finish immediately."
        ),
    )


@router.post(
    "/categorize-untagged",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def categorize_untagged(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
    limit: int = Query(
        default=_DEFAULT_BATCH_LIMIT,
        ge=1,
        le=_MAX_BATCH_LIMIT,
        description=(
            f"Max untagged laptops to tag in this run (default {_DEFAULT_BATCH_LIMIT}). "
            f"Hard ceiling is {_MAX_BATCH_LIMIT} to stay within the Gemma 4 "
            "free-tier daily quota (1500 RPD)."
        ),
    ),
) -> JobAccepted:
    """
    Backfill category tags for laptops with no `laptop_categories` link.

    **Returns 202 immediately** — one throttled LLM call per laptop. The
    prompt is much smaller than extraction's, so this is paced faster and a
    batch of 100 runs in roughly 8 minutes. Poll `poll_url` for progress.

    Tagging is additive: manually assigned tags are never removed, so this is
    safe to re-run until the job result reports `untagged_remaining: 0`.
    """
    queued = len(
        session.exec(
            select(Laptop.id)
            .where(Laptop.id.not_in(select(LaptopCategory.laptop_id)))  # type: ignore[union-attr]
            .limit(limit)
        ).all()
    )

    job = create_job(
        session,
        job_type=JobType.CATEGORIZE_UNTAGGED,
        total_count=queued,
        params={"limit": limit},
        created_by=current_admin.id,
        seconds_per_item=_SECONDS_PER_TAG_ITEM,
    )

    def worker(work_session: Session, progress: JobProgress) -> dict:
        return categorize_untagged_laptops(work_session, limit=limit, progress=progress)

    background_tasks.add_task(run_job, job.id, worker)

    return job_accepted(
        job,
        message=(
            f"Tagging {queued} untagged laptop(s) in the background."
            if queued
            else "No untagged laptops found — the job will finish immediately."
        ),
    )
