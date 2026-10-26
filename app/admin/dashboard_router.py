"""
GET /admin/dashboard/summary: everything the admin Dashboard shows, in one
request and one session, from SQL aggregates (count / max / min / GROUP BY).

The Dashboard answers three questions: is anything broken, does anything need
me, is the data fresh. Dates are returned raw; the frontend applies the
amber/red thresholds, so changing a threshold is not a backend deploy.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session, select

from app.agent.monitoring_models import AgentRunLog
from app.common.app_settings import REVIEW_PAUSE_REASON_KEY, get_setting_row
from app.common.job_model import BackgroundJob, JobRead, JobStatus
from app.config import settings
from app.database import get_session
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopEmbedding, LaptopPickScore, LaptopStatus
from app.reviews import funnel
from app.reviews.models import RawYoutubeReview, ReviewStatus
from app.reviews.link_model import ReviewLaptopLink
from app.scraper.bulk_scraper import HTML_UPLOAD_BRANDS
from app.scraper.models import RawScrapLaptop
from app.scraper.raw_html_model import RawProductHtml
from app.users.auth import get_current_admin

router = APIRouter(
    prefix="/admin/dashboard",
    tags=["Admin - Dashboard"],
    dependencies=[Depends(get_current_admin)],
)

_UNVERIFIED_FLAGS = ("cpu_benchmark_unresolved", "gpu_benchmark_unresolved")


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Some timestamp columns are naive UTC; return every date aware."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.get("/summary")
def dashboard_summary(session: Session = Depends(get_session)) -> dict:
    now = datetime.now(timezone.utc)
    active = Laptop.status == LaptopStatus.ACTIVE.value

    # --- Needs attention ------------------------------------------------
    week_ago = now - timedelta(days=7)
    failed_jobs, item_failures = session.exec(
        select(
            func.count(BackgroundJob.id).filter(BackgroundJob.status == JobStatus.FAILED),
            # A run that finishes is `completed` even if every item failed.
            func.count(BackgroundJob.id).filter(
                BackgroundJob.status == JobStatus.COMPLETED, BackgroundJob.failed_count > 0
            ),
        ).where(BackgroundJob.created_at >= week_ago)
    ).one()

    # Laptops, not rows: one laptop has a row per use case.
    flags = cast(LaptopPickScore.flags, JSONB)
    unverified_flag = or_(*(flags[f].as_boolean() for f in _UNVERIFIED_FLAGS))
    scored, generated, unverified = session.exec(
        select(
            func.count(func.distinct(LaptopPickScore.laptop_id)).filter(LaptopPickScore.score.is_not(None)),  # type: ignore[union-attr]
            func.count(func.distinct(LaptopPickScore.laptop_id)),
            func.count(func.distinct(LaptopPickScore.laptop_id)).filter(unverified_flag),
        )
        .join(Laptop, Laptop.id == LaptopPickScore.laptop_id)
        .where(active)
    ).one()

    counts = dict(session.exec(select(Laptop.status, func.count(Laptop.id)).group_by(Laptop.status)).all())
    awaiting_price = session.exec(
        select(func.count(Laptop.id)).where(
            Laptop.status == LaptopStatus.INACTIVE.value, Laptop.price_rm == 0
        )
    ).one()

    # Same predicate as GET /reviews/pending: every pending review, plus
    # matched reviews with no link.
    has_link = select(ReviewLaptopLink.id).where(ReviewLaptopLink.raw_review_id == RawYoutubeReview.id).exists()
    reviews_to_link = session.exec(
        select(func.count(RawYoutubeReview.id)).where(
            or_(
                RawYoutubeReview.status == ReviewStatus.PENDING.value,
                (RawYoutubeReview.status == ReviewStatus.MATCHED.value) & ~has_link,
            )
        )
    ).one()

    pause = get_setting_row(session, REVIEW_PAUSE_REASON_KEY)

    # --- Freshness ----------------------------------------------------------
    scraped = dict(session.exec(
        select(RawScrapLaptop.brand_id, func.max(RawScrapLaptop.created_at)).group_by(RawScrapLaptop.brand_id)
    ).all())
    uploaded = dict(session.exec(
        select(RawProductHtml.brand_id, func.max(RawProductHtml.updated_at)).group_by(RawProductHtml.brand_id)
    ).all())
    brands = session.exec(select(LaptopBrand).order_by(LaptopBrand.name, LaptopBrand.id)).all()
    scrape_by_brand = []
    for b in brands:
        upload = b.name.lower() in HTML_UPLOAD_BRANDS
        scrape_by_brand.append({
            "brand_id": b.id,
            "brand_name": b.name,
            # Data arrival, not last_scraped_at: that is stamped on skipped
            # targets too. Acer is saved by hand, so its date is the upload.
            "source": "upload" if upload else "scrape",
            "last_at": _utc((uploaded if upload else scraped).get(b.id)),
        })

    pickscore_at = session.exec(select(func.max(LaptopPickScore.updated_at))).one()
    catalog_changed_at = session.exec(select(func.max(Laptop.updated_at))).one()
    embeddings_at = session.exec(select(func.max(LaptopEmbedding.updated_at))).one()
    oldest_check, unchecked = session.exec(
        select(
            func.min(Laptop.price_checked_at),
            func.count(Laptop.id).filter(Laptop.price_checked_at.is_(None)),  # type: ignore[union-attr]
        ).where(active)
    ).one()

    # --- Jobs -------------------------------------------------------------
    running = session.exec(
        select(BackgroundJob)
        .where(BackgroundJob.status.in_(list(JobStatus.ACTIVE)))  # type: ignore[attr-defined]
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id)  # type: ignore[attr-defined]
    ).all()
    recent = session.exec(
        select(BackgroundJob)
        .where(BackgroundJob.status.in_(list(JobStatus.TERMINAL)))  # type: ignore[attr-defined]
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id)  # type: ignore[attr-defined]
        .limit(5)
    ).all()

    # --- Catalog shares (active laptops only) -------------------------------
    has_embedding = select(LaptopEmbedding.id).where(LaptopEmbedding.laptop_id == Laptop.id).exists()
    with_embedding, with_reviews = session.exec(
        select(
            func.count(Laptop.id).filter(has_embedding),
            func.count(Laptop.id).filter(funnel.shows_reviews()),
        ).where(active)
    ).one()

    # --- Pico: rolling windows, not calendar days ----------------------------
    # agent_run_logs.created_at is naive UTC. A conversation counts when it
    # had at least one Pico turn in the window.
    naive = now.replace(tzinfo=None)

    def pico(since: datetime) -> tuple[int, int, int, int]:
        return session.exec(
            select(
                func.count(func.distinct(AgentRunLog.conversation_id)),
                func.count(AgentRunLog.id),
                func.count(AgentRunLog.id).filter(AgentRunLog.status == "error"),
                func.count(AgentRunLog.id).filter(AgentRunLog.fallback_triggered.is_(True)),  # type: ignore[attr-defined]
            ).where(AgentRunLog.created_at >= since)
        ).one()

    conv_24h, runs_24h, errors_24h, fallbacks_24h = pico(naive - timedelta(hours=24))
    conv_7d, runs_7d, errors_7d, fallbacks_7d = pico(naive - timedelta(days=7))

    return {
        "generated_at": now,
        "attention": {
            "failed_jobs_7d": failed_jobs,
            "jobs_with_item_failures_7d": item_failures,
            "review_processing": {
                "paused": settings.review_processing_paused,
                "reason": pause.value if pause else None,
            },
            "pickscore": {
                "missing": counts.get(LaptopStatus.ACTIVE.value, 0) - generated,
                "withheld": generated - scored,
                "unverified": unverified,
            },
            "awaiting_price": awaiting_price,
            "reviews_to_link": reviews_to_link,
            "pico_24h": {"runs": runs_24h, "errors": errors_24h, "fallbacks": fallbacks_24h},
        },
        "freshness": {
            "scrape_by_brand": scrape_by_brand,
            # No record of a PassMark refresh exists until step 10 adds one.
            "passmark_refreshed_at": None,
            "pickscore_recalculated_at": _utc(pickscore_at),
            # Amber when pickscore_recalculated_at is older than this.
            "catalog_changed_at": _utc(catalog_changed_at),
            "embeddings_updated_at": _utc(embeddings_at),
            "price_checked_oldest_at": _utc(oldest_check),
            "price_checked_missing": unchecked,
        },
        "jobs": {
            "running": [JobRead.from_job(j) for j in running],
            "recent": [JobRead.from_job(j) for j in recent],
        },
        "catalog": {
            "active": counts.get(LaptopStatus.ACTIVE.value, 0),
            "inactive": counts.get(LaptopStatus.INACTIVE.value, 0),
            "suspended": counts.get(LaptopStatus.SUSPENDED.value, 0),
            "active_with_pickscore": generated,
            "active_with_embedding": with_embedding,
            "active_with_reviews": with_reviews,
        },
        "pico": {
            "conversations_24h": conv_24h,
            "conversations_7d": conv_7d,
            "runs_24h": runs_24h,
            "runs_7d": runs_7d,
            "errors_24h": errors_24h,
            "errors_7d": errors_7d,
            "fallbacks_24h": fallbacks_24h,
            "fallbacks_7d": fallbacks_7d,
        },
    }
