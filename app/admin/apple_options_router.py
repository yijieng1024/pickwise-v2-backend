"""
Admin view of the Apple build-to-order options (step 10c, step (h)).

    GET  /admin/apple-options         every active Apple standard config's chip kits, when
                                      they were fetched, and the latest fetch's problems
    POST /admin/apple-options/fetch   202 + job: fetch the options only (no buy page scrape)

Read-only otherwise: Apple's configurator API is the only source (plan decision 11).
The fetch problems (`chip_not_offered`, `options_failed`, `stopped`) are not stored on
the rows; they come from the latest job that ran the options step: a fetch-now job, or
an Apple bulk scrape / scrape-targets run whose result carries `apple_options`.

One lock for both ways in: a fetch-now job refuses while an Apple scrape is active, and
an Apple scrape refuses while a fetch-now job is (app/scraper/router.py). A deploy
interrupts a running job (reset_stale_jobs fails it); the step writes only at the end,
so the stored kits are kept.
"""
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session, select

from app.common.job_model import BackgroundJob, JobAccepted, JobStatus, JobType
from app.common.job_service import JobProgress, create_job, job_accepted, run_job
from app.database import get_session
from app.laptops import options_service as svc
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.scraper.apple_options_fetch import apple_brand_ids, run_options_step
from app.scraper.models import ScrapeTarget
from app.users.auth import get_current_admin
from app.users.models import User

router = APIRouter(prefix="/admin/apple-options", tags=["Admin - Apple options"],
                   dependencies=[Depends(get_current_admin)])

# Measured 2026-11-05 (backfill dry run against Apple): 33 calls, 16 chip kits, 249 s.
FETCH_SECONDS = 250
_SCRAPES = (JobType.BULK_SCRAPE, JobType.SCRAPE_TARGETS)
_RESULT_SCAN = 50


def is_apple_scrape(session: Session, job: BackgroundJob) -> bool:
    """A scrape job that would run the options step: Apple's bulk scrape, or selected
    targets that include an Apple one."""
    params = job.params or {}
    if job.job_type == JobType.BULK_SCRAPE:
        return (params.get("brand_name") or "").lower() == "apple"
    ids = params.get("target_ids") or []
    if not ids:
        return False
    brands = apple_brand_ids(session)
    return any(t.brand_id in brands for t in session.exec(
        select(ScrapeTarget).where(ScrapeTarget.id.in_(ids))))  # type: ignore[attr-defined]


def active_apple_job(session: Session) -> Optional[BackgroundJob]:
    """A fetch-now job or an Apple scrape that is queued or running."""
    jobs = session.exec(select(BackgroundJob).where(
        BackgroundJob.job_type.in_([JobType.APPLE_OPTIONS_FETCH, *_SCRAPES]),  # type: ignore[attr-defined]
        BackgroundJob.status.in_(list(JobStatus.ACTIVE)))).all()  # type: ignore[attr-defined]
    return next((j for j in jobs if j.job_type == JobType.APPLE_OPTIONS_FETCH or is_apple_scrape(session, j)), None)


def _conflict(job: BackgroundJob, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT,
                         detail={"code": "job_running", "message": message, "job_id": str(job.id)})


def _latest_options_run(session: Session) -> Optional[dict[str, Any]]:
    jobs = session.exec(select(BackgroundJob).where(
        BackgroundJob.job_type.in_([JobType.APPLE_OPTIONS_FETCH, *_SCRAPES]),  # type: ignore[attr-defined]
        BackgroundJob.status == JobStatus.COMPLETED,
    ).order_by(BackgroundJob.finished_at.desc()).limit(_RESULT_SCAN)).all()  # type: ignore[union-attr]
    for job in jobs:
        options = (job.result or {}).get("apple_options")
        if options:
            return {"job_id": str(job.id), "job_type": job.job_type, "finished_at": job.finished_at,
                    "error": options.get("error"),
                    **{k: options.get(k) for k in ("chip_kits", "apple_calls", "stopped",
                                                    "chip_not_offered", "options_failed")}}
    return None


@router.get("")
def options_view(session: Session = Depends(get_session)) -> dict:
    apple = apple_brand_ids(session)
    laptops = session.exec(select(Laptop).where(
        Laptop.brand_id.in_(apple), Laptop.status == LaptopStatus.ACTIVE.value)  # type: ignore[attr-defined]
        .order_by(Laptop.product_name, Laptop.id)).all()
    configs = []
    for laptop in laptops:
        if not laptop.part_numbers:
            continue  # not a standard config on the buy page
        entry = {"laptop_id": laptop.id, "product_name": laptop.product_name, "price_rm": laptop.price_rm}
        try:
            c = svc.configurable(session, laptop.id)
        except svc.NotConfigurable:
            configs.append({**entry, "standard_chip": None, "kits": [], "fetched_at": None})
            continue
        configs.append({**entry, "standard_chip": c.chip, "standard": c.standard,
                        "fetched_at": max((r["fetched_at"] for r in c.rows), default=None),
                        "kits": svc.kits_listing(c)})
    active = active_apple_job(session)
    return {"configs": configs, "latest_run": _latest_options_run(session),
            "running_job_id": str(active.id) if active else None}


@router.post("/fetch", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def fetch_now(background_tasks: BackgroundTasks, session: Session = Depends(get_session),
                    current_admin: User = Depends(get_current_admin)) -> JobAccepted:
    """Fetch every standard config's options from Apple now, from the stored buy pages
    (no buy page scrape first). The job's result is {"apple_options": <summary>}."""
    if active := active_apple_job(session):
        raise _conflict(active, "An Apple options fetch or Apple scrape is already running.")
    job = create_job(session, job_type=JobType.APPLE_OPTIONS_FETCH, total_count=1,
                     created_by=current_admin.id, seconds_per_item=FETCH_SECONDS, overhead_seconds=0)

    async def worker(work_session: Session, progress: JobProgress) -> dict:
        result = {"apple_options": await run_options_step(work_session)}
        stopped = result["apple_options"].get("stopped")
        progress.advance(succeeded=not stopped, item="apple", error=str(stopped) if stopped else None)
        return result

    background_tasks.add_task(run_job, job.id, worker)
    return job_accepted(job, message="Fetching Apple build-to-order options in the background.")
