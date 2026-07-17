import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.database import get_session
from app.logger import get_logger
from app.reviews.aggregator import aggregate_for_laptop
from app.reviews.discovery import resolve_channel_from_url
from app.reviews.models import (
    ManualMatchRequest,
    RawYoutubeReview,
    RawYoutubeReviewRead,
    YoutubeChannel,
    YoutubeChannelCreate,
    YoutubeChannelUpdate,
)
from app.reviews.matcher import match_laptop
from app.reviews.processor import process_raw_review
from app.reviews.service import ingest_bulk, ingest_for_laptop
from app.users.auth import get_current_admin

router = APIRouter(prefix="/reviews", tags=["Reviews"])
logger = get_logger(__name__)


# --- Channel management ---

@router.post("/channels", status_code=201)
def add_channel(
    body: YoutubeChannelCreate,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    try:
        resolved = resolve_channel_from_url(body.channel_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = session.exec(
        select(YoutubeChannel).where(
            YoutubeChannel.channel_id == resolved["channel_id"]
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Channel already exists.")

    channel = YoutubeChannel(
        channel_id=resolved["channel_id"],
        channel_name=resolved["channel_name"],
        channel_img_url=resolved["channel_img_url"],
        trust_tier=body.trust_tier,
        active=body.active,
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


@router.get("/channels")
def list_channels(
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    return session.exec(select(YoutubeChannel)).all()


@router.patch("/channels/{channel_id}")
def update_channel(
    channel_id: uuid.UUID,
    body: YoutubeChannelUpdate,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    channel = session.get(YoutubeChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(channel, field, value)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


# --- Ingestion pipeline ---

@router.post("/ingest/{laptop_id}")
def ingest_laptop(
    laptop_id: uuid.UUID,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Trigger full discovery + transcript + matching pipeline for one laptop."""
    try:
        counts = ingest_for_laptop(laptop_id, session)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return counts


@router.post("/ingest-bulk")
def ingest_bulk_endpoint(
    limit: int = Query(
        default=5,
        ge=1,
        le=20,
        description=(
            "Max laptop families to search this run. Each family costs "
            "~active_channels × 100 YouTube quota units (daily quota: 10,000 — "
            "with 11 channels that's ~9 families/day)."
        ),
    ),
    skip_covered: bool = Query(
        default=True,
        description="Skip families that already have a matched raw review.",
    ),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """
    Bulk discovery: runs discovery + transcript + matching across the catalog,
    one YouTube search per laptop *family* (config variants collapsed — a
    reviewer covers 'TUF Gaming F15', not each RAM/SSD configuration).
    Re-run daily with skip_covered=true to walk the catalog within quota.
    """
    try:
        return ingest_bulk(session, limit=limit, skip_covered=skip_covered)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- Raw review management ---

@router.get("/raw", response_model=list[RawYoutubeReviewRead])
def list_raw_reviews(
    status: str | None = None,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    stmt = select(RawYoutubeReview)
    if status:
        stmt = stmt.where(RawYoutubeReview.status == status)
    return session.exec(stmt).all()


@router.patch("/raw/{review_id}/match")
def manual_match(
    review_id: uuid.UUID,
    body: ManualMatchRequest,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Manually pair a low-confidence raw review to a specific laptop."""
    review = session.get(RawYoutubeReview, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")
    review.matched_laptop_id = body.laptop_id
    review.match_confidence = 100.0
    review.status = "matched"
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


@router.post("/rematch")
def rematch_pending(
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Re-run auto-matching on all pending raw reviews using the current matcher config."""
    pending = session.exec(
        select(RawYoutubeReview).where(RawYoutubeReview.status == "pending")
    ).all()

    updated = 0
    for review in pending:
        laptop_id, confidence = match_laptop(review.video_title, session)
        if laptop_id:
            review.matched_laptop_id = laptop_id
            review.match_confidence = confidence
            review.status = "matched"
            session.add(review)
            updated += 1

    session.commit()
    logger.info("Rematch: %d of %d pending reviews matched", updated, len(pending))
    return {"pending_total": len(pending), "newly_matched": updated}


# --- Chunking / embedding ---

@router.post("/process/{review_id}")
def process_review(
    review_id: uuid.UUID,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Run chunking, sentiment tagging, and embedding for a matched raw review."""
    try:
        chunks_saved = process_raw_review(review_id, session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"chunks_saved": chunks_saved}


# --- Aggregation ---

@router.post("/aggregate/{laptop_id}")
def aggregate_laptop(
    laptop_id: uuid.UUID,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Recompute the laptop_review_summary for a given laptop from all its chunks."""
    summary = aggregate_for_laptop(laptop_id, session)
    return {
        "laptop_id": summary.laptop_id,
        "review_count": summary.review_count,
        "strengths": summary.aggregated_strengths,
        "weaknesses": summary.aggregated_weaknesses,
    }
