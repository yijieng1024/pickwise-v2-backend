from collections import Counter
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
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


class CoverageHistoryDay(BaseModel):
    """One day of the coverage chart. Both counts are cumulative as of that day."""

    date: date
    catalog_total: int
    embedded_total: int


class CoverageHistory(BaseModel):
    days: list[CoverageHistoryDay]


# Ceiling on the window. Aggregation is done in Python over every laptop row
# rather than in SQL — the same call made in agent monitoring's /stats, and
# correct at a catalog of a few hundred.
MAX_COVERAGE_HISTORY_DAYS = 365


def _as_utc_date(value: datetime) -> date:
    """Naive datetimes come out of a TIMESTAMP WITHOUT TIME ZONE column; they
    are written as UTC, so read them back that way rather than as server-local."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).date()
    return value.astimezone(timezone.utc).date()


@router.get(
    "/laptops/coverage-history",
    response_model=CoverageHistory,
    dependencies=[Depends(get_current_admin)],
)
def get_coverage_history(
    days: int = Query(default=90, ge=1, le=MAX_COVERAGE_HISTORY_DAYS),
    session: Session = Depends(get_session),
):
    """
    Search coverage against catalog size, day by day — the shape behind the
    single percentage `GET /laptops/status` returns.

    **Read it as cohorts, not as a work log.** Laptops are bucketed by the day
    they entered the catalog (`Laptop.created_at`), and `embedded_total` counts
    how many of those have a vector *right now*. So the gap between the two
    lines at the right-hand edge is today's missing count, and *where* the gap
    opens says which laptops are unsearchable: a gap only at the recent end is
    the normal lag after a scrape, while one that opens further back is a
    backlog no run ever cleared.

    WHY NOT bucket by when each embedding was written, which would be the more
    obvious "work done over time" chart: `LaptopEmbedding` has only
    `updated_at`, and `upsert_laptop_embedding` overwrites it every time a
    vector is refreshed. One full re-run would restamp every row with today's
    date and flatten all history into a single vertical jump. `created_at` on
    the laptop is never rewritten, so this framing survives re-runs — and the
    per-run history is already on `GET /jobs?job_type=embeddings.generate_all`.

    Days are UTC, matching `runs_today` in agent monitoring.
    """
    rows = session.execute(
        select(Laptop.created_at, LaptopEmbedding.id).join(
            LaptopEmbedding,
            LaptopEmbedding.laptop_id == Laptop.id,
            isouter=True,
        )
    ).all()

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)

    # Everything already in the catalog when the window opens — without this
    # baseline both lines would start at zero and draw the catalog as if it had
    # been built entirely inside the window.
    catalog_running = 0
    embedded_running = 0
    added: Counter = Counter()
    embedded_added: Counter = Counter()

    for created_at, embedding_id in rows:
        created = _as_utc_date(created_at)
        if created < start:
            catalog_running += 1
            if embedding_id is not None:
                embedded_running += 1
        elif created <= today:
            added[created] += 1
            if embedding_id is not None:
                embedded_added[created] += 1
        # Rows dated in the future are left out of both — nothing sensible to
        # do with a clock-skewed timestamp on a cumulative axis.

    out: list[CoverageHistoryDay] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        catalog_running += added.get(day, 0)
        embedded_running += embedded_added.get(day, 0)
        out.append(
            CoverageHistoryDay(
                date=day,
                catalog_total=catalog_running,
                embedded_total=embedded_running,
            )
        )

    return CoverageHistory(days=out)
