from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlmodel import Session

from app.common.job_model import JobAccepted, JobType
from app.common.job_service import JobProgress, create_job, job_accepted, run_job
from app.database import get_session
from app.users.auth import get_current_admin
from app.users.models import User
from app.laptops.laptop_models import Laptop, LaptopEmbedding
from app.embeddings.service import (
    generate_all_laptop_embeddings,
    generate_single_laptop_embedding,
)

router = APIRouter(prefix="/embeddings", tags=["Embeddings"])

# Measured: one Gemini embedding call plus the 0.3s courtesy sleep in the
# service. Only feeds the 202's ETA — the job's real progress comes from the
# per-item advances.
_SECONDS_PER_EMBEDDING = 1.0


@router.post(
    "/laptops/generate-all",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_generate_all(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
) -> JobAccepted:
    """
    Start embedding every laptop in the catalog.

    **Returns 202 immediately — it does not wait for the work.** Each laptop is
    one throttled Gemini call, so a full catalog run takes minutes.

    Poll `GET /api/v2/jobs/{job_id}` (returned as `poll_url`) for live counts
    and per-item errors, the same as the scraper and processor. Before this,
    the run left no job record at all: the only signal was watching the
    embedded count climb on `GET /embeddings/laptops/status`, which meant
    progress could not survive a page reload and a crashed run was
    indistinguishable from a finished one.

    `GET /embeddings/laptops/status` still works and is still the right thing
    for overall coverage — it is just no longer the only way to follow a run.

    Admin-only: external API calls cost money and could hit rate limits if
    triggered carelessly.
    """
    total = session.execute(select(func.count()).select_from(Laptop)).scalar() or 0

    job = create_job(
        session,
        job_type=JobType.GENERATE_EMBEDDINGS,
        total_count=total,
        created_by=current_admin.id,
        # One Gemini call plus the service's 0.3s courtesy sleep.
        seconds_per_item=_SECONDS_PER_EMBEDDING,
    )

    def worker(work_session: Session, progress: JobProgress) -> dict:
        return generate_all_laptop_embeddings(work_session, progress)

    # `run_job` opens its own sessions. The previous version handed the
    # request's session to the background task, which FastAPI closes once the
    # response is sent — the task was working on a dead session.
    background_tasks.add_task(run_job, job.id, worker)

    return job_accepted(
        job,
        message=f"Embedding {total} laptop(s) — poll the job for progress.",
    )


@router.post("/laptops/{laptop_id}", dependencies=[Depends(get_current_admin)])
def trigger_generate_single(
    laptop_id: UUID,
    session: Session = Depends(get_session),
):
    """
    Generates or refreshes the embedding for a single laptop immediately.

    WHY synchronous here (no BackgroundTasks):
    One API call takes ~1 second — fast enough to return the result directly.
    The caller gets immediate confirmation of success or failure, which is
    more useful than a deferred background task for a single item.
    """
    result = generate_single_laptop_embedding(session, laptop_id)
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.get("/laptops/status")
def get_embedding_status(session: Session = Depends(get_session)):
    """
    Shows how many laptops have been embedded vs. total.

    WHY this endpoint exists:
    Since generate-all runs in the background with no progress stream,
    you need a way to check completion. This gives you a live count of
    embedded vs. missing laptops so you know when the job is done.
    """
    total_laptops = session.execute(select(func.count()).select_from(Laptop)).scalar()
    total_embedded = session.execute(select(func.count()).select_from(LaptopEmbedding)).scalar()
    missing = total_laptops - total_embedded

    return {
        "total_laptops": total_laptops,
        "embedded": total_embedded,
        "missing": missing,
        "coverage_pct": round(total_embedded / total_laptops * 100, 1) if total_laptops > 0 else 0,
    }
