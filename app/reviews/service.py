import uuid
from datetime import datetime, timezone

from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
# The single definition of the model-line key, shared with the laptop_family
# grouping (app/laptops/family_service.py). Two copies drifted apart is the
# whole reason it moved out of this module: this pipeline searches YouTube
# once per key, family grouping seeds one product line per key, and a key that
# means two different things in two places is a bug nobody would notice.
from app.laptops.family_key import family_key
from app.laptops.laptop_models import Laptop
from app.logger import get_logger
from app.reviews.discovery import discover_videos
from app.reviews.matcher import match_laptop
from app.reviews.models import RawYoutubeReview, YoutubeChannel
from app.reviews.transcript import fetch_transcript

logger = get_logger(__name__)


def ingest_bulk(session: Session, limit: int = 5, skip_covered: bool = True) -> dict:
    """
    Run the discovery + transcript + match pipeline across the catalog, one
    search per laptop *family* (see family_key). Discovered videos are
    matched against the whole catalog by the matcher, so one family search
    can populate raw reviews for several variants.

    skip_covered=True skips families that already have at least one matched
    raw review, so repeated runs walk through the catalog day by day within
    the YouTube quota (cost ≈ active_channels × 100 units per family).
    Chunking/embedding stays a separate step (POST /reviews/process/{id}).
    """
    active_channels = session.exec(
        select(YoutubeChannel).where(YoutubeChannel.active == True)  # noqa: E712
    ).all()
    if not active_channels:
        return {
            "message": "No active YouTube channels registered — add channels first.",
            "families_attempted": 0,
            "results": [],
        }

    laptops = session.exec(select(Laptop)).all()
    families: dict[str, Laptop] = {}
    for laptop in laptops:
        families.setdefault(family_key(laptop.product_name), laptop)

    covered: set[str] = set()
    if skip_covered:
        matched_laptops = session.exec(
            select(Laptop)
            .join(RawYoutubeReview, RawYoutubeReview.matched_laptop_id == Laptop.id)  # type: ignore[arg-type]
        ).all()
        covered = {family_key(l.product_name) for l in matched_laptops}

    todo = [(key, laptop) for key, laptop in families.items() if key not in covered]
    todo = todo[:limit]

    results = []
    totals = {"discovered": 0, "skipped": 0, "matched": 0, "pending": 0, "rejected": 0}
    for key, laptop in todo:
        try:
            counts = ingest_for_laptop(laptop.id, session)
            for k in totals:
                totals[k] += counts.get(k, 0)
            results.append({"family": key, "queried_laptop_id": str(laptop.id), **counts})
        except Exception as e:
            logger.error("Bulk ingest failed for family '%s': %s", key, e)
            results.append({"family": key, "queried_laptop_id": str(laptop.id), "error": str(e)})

    return {
        "families_total": len(families),
        "families_already_covered": len(covered),
        "families_attempted": len(todo),
        "families_remaining": max(0, len(families) - len(covered) - len(todo)),
        "estimated_quota_units_used": len(todo) * len(active_channels) * 100,
        "totals": totals,
        "results": results,
    }


def ingest_for_laptop(laptop_id: uuid.UUID, session: Session) -> dict:
    """
    Run the full discovery + transcript-fetch + matching pipeline for one laptop.
    Stages:
      1. Discover videos across all active channels
      2. Skip videos already in raw_youtube_reviews
      3. Fetch transcript for each new video
      4. Fuzzy-match video title against the laptop catalog
      5. Persist to raw_youtube_reviews (status: matched | pending | rejected)

    Returns a summary dict with counts for each outcome.
    Chunking/embedding is a separate step — call process_raw_review() per matched review.
    """
    laptop_row = session.execute(
        sa_select(Laptop, LaptopBrand.name).join(
            LaptopBrand, LaptopBrand.id == Laptop.brand_id
        ).where(Laptop.id == laptop_id)
    ).first()

    if not laptop_row:
        raise ValueError(f"Laptop {laptop_id} not found.")

    laptop, brand_name = laptop_row
    channels = session.exec(
        select(YoutubeChannel).where(YoutubeChannel.active == True)  # noqa: E712
    ).all()

    if not channels:
        return {"discovered": 0, "skipped": 0, "matched": 0, "pending": 0, "rejected": 0}

    videos = discover_videos(brand_name, laptop.product_name, channels)
    logger.info("Discovered %d videos for laptop %s", len(videos), laptop.product_name)

    counts = {"discovered": len(videos), "skipped": 0, "matched": 0, "pending": 0, "rejected": 0}

    for video in videos:
        existing = session.exec(
            select(RawYoutubeReview).where(
                RawYoutubeReview.video_id == video["video_id"]
            )
        ).first()
        # Skip already-processed videos; retry rejected ones (may have failed due to transient errors)
        if existing and existing.status != "rejected":
            counts["skipped"] += 1
            continue

        segments = fetch_transcript(video["video_id"])

        if segments is None:
            if existing:
                counts["skipped"] += 1  # still no transcript — leave as rejected
            else:
                raw = RawYoutubeReview(
                    video_id=video["video_id"],
                    channel_id=video["channel_id"],
                    video_title=video["video_title"],
                    published_at=datetime.fromisoformat(
                        video["published_at"].replace("Z", "+00:00")
                    ) if video.get("published_at") else None,
                    raw_transcript={},
                    status="rejected",
                )
                session.add(raw)
                counts["rejected"] += 1
            continue

        matched_laptop_id, confidence = match_laptop(video["video_title"], session)
        status = "matched" if matched_laptop_id else "pending"

        if existing:
            # Upgrade the previously-rejected row now that we have a transcript
            existing.raw_transcript = {"segments": segments}
            existing.matched_laptop_id = matched_laptop_id
            existing.match_confidence = confidence
            existing.status = status
            session.add(existing)
        else:
            raw = RawYoutubeReview(
                video_id=video["video_id"],
                channel_id=video["channel_id"],
                video_title=video["video_title"],
                published_at=datetime.fromisoformat(
                    video["published_at"].replace("Z", "+00:00")
                ) if video.get("published_at") else None,
                raw_transcript={"segments": segments},
                matched_laptop_id=matched_laptop_id,
                match_confidence=confidence,
                status=status,
            )
            session.add(raw)
        counts[status] += 1

    session.commit()
    logger.info("Ingest complete for laptop %s: %s", laptop.product_name, counts)
    return counts
