import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.logger import get_logger
from app.reviews.models import LaptopReviewChunk, LaptopReviewSummary

logger = get_logger(__name__)

_MAX_PER_SENTIMENT = 5


def aggregate_for_laptop(laptop_id: uuid.UUID, session: Session) -> LaptopReviewSummary:
    """
    Roll up all processed chunks for a laptop into laptop_review_summary.
    Deduplicates by selecting the top _MAX_PER_SENTIMENT distinct chunk texts per sentiment.
    Creates a new summary row if one doesn't exist; updates in-place otherwise.
    """
    chunks = session.exec(
        select(LaptopReviewChunk).where(LaptopReviewChunk.laptop_id == laptop_id)
    ).all()

    strengths = list(
        dict.fromkeys(c.chunk_text for c in chunks if c.sentiment_tag == "strength")
    )[:_MAX_PER_SENTIMENT]
    weaknesses = list(
        dict.fromkeys(c.chunk_text for c in chunks if c.sentiment_tag == "weakness")
    )[:_MAX_PER_SENTIMENT]
    review_count = len({c.video_id for c in chunks})

    summary = session.exec(
        select(LaptopReviewSummary).where(LaptopReviewSummary.laptop_id == laptop_id)
    ).first()

    if summary:
        summary.aggregated_strengths = strengths
        summary.aggregated_weaknesses = weaknesses
        summary.review_count = review_count
        summary.last_updated_at = datetime.now(timezone.utc)
    else:
        summary = LaptopReviewSummary(
            laptop_id=laptop_id,
            aggregated_strengths=strengths,
            aggregated_weaknesses=weaknesses,
            review_count=review_count,
        )
        session.add(summary)

    session.commit()
    session.refresh(summary)
    logger.info(
        "Aggregated laptop %s: %d strengths, %d weaknesses, %d videos",
        laptop_id,
        len(strengths),
        len(weaknesses),
        review_count,
    )
    return summary
