import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.logger import get_logger
from app.reviews.models import (
    LaptopReviewChunk,
    LaptopReviewSummary,
    YoutubeChannel,
)

logger = get_logger(__name__)

_MAX_PER_SENTIMENT = 5


def aggregate_for_laptop(laptop_id: uuid.UUID, session: Session) -> LaptopReviewSummary:
    """
    Roll up all processed chunks for a laptop into laptop_review_summary.
    Deduplicates by selecting the top _MAX_PER_SENTIMENT distinct chunk texts per sentiment.
    Creates a new summary row if one doesn't exist; updates in-place otherwise.
    """
    # Explicit ordering, and it is load-bearing rather than tidiness: the
    # "top 5" below is just the first five distinct texts this query returns,
    # so with no ORDER BY it was Postgres's return order. Two aggregations of
    # unchanged data could produce two different summaries, and nothing would
    # indicate which one a user saw.
    #
    # evidence_tier first (tier_1 before tier_2, which `asc()` gives for free
    # on these strings), so a reviewer who actually benchmarks the machine
    # leads the summary. Then newest first — a 2026 review of a 2026 laptop
    # beats a 2024 one. Then `id`, because the first two still tie constantly:
    # every chunk of one video shares a channel and is written in one batch,
    # so a unique final key is what makes the order total. Without it the
    # ordering is deterministic only down to the tied block, and the whole
    # point of this change is a repeat run producing identical output.
    #
    # market_relevance and review_language are deliberately NOT sort keys,
    # although they sit right next to evidence_tier and will look like obvious
    # ones. Neither says anything about whether a strength claim is well
    # evidenced: a Taiwanese channel can be the best evidence in the corpus,
    # and "thermals hold under sustained load" is true regardless of which
    # currency the reviewer quotes. Market relevance matters for PRICE claims,
    # which is a chunk-scope problem, not an ordering one — reordering cannot
    # remove a foreign price figure from the pool, only rank it lower. That
    # belongs with ADR-0012's scope mechanism.
    chunks = session.exec(
        select(LaptopReviewChunk)
        .join(
            YoutubeChannel,
            YoutubeChannel.channel_name == LaptopReviewChunk.channel_name,
            # LEFT JOIN: chunks store channel_name, not channel_id, so a
            # renamed or deregistered channel would otherwise drop its chunks
            # out of the summary entirely. Losing evidence is worse than
            # ranking it last, which is what the NULLS LAST below does.
            isouter=True,
        )
        .where(LaptopReviewChunk.laptop_id == laptop_id)
        .order_by(
            YoutubeChannel.evidence_tier.asc().nullslast(),  # type: ignore[union-attr]
            LaptopReviewChunk.created_at.desc(),  # type: ignore[attr-defined]
            LaptopReviewChunk.id,
        )
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
