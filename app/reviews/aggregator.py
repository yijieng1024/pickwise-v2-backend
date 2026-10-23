import uuid
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlmodel import Session, select

from app.laptops.laptop_models import Laptop
from app.logger import get_logger
from app.reviews.models import (
    ChunkScope,
    LaptopReviewChunk,
    LaptopReviewSummary,
    RawYoutubeReview,
    SHOWN_CHUNKS,
    YoutubeChannel,
)

logger = get_logger(__name__)

_MAX_PER_SENTIMENT = 5


def _ordered_chunks(session: Session, where) -> list[LaptopReviewChunk]:
    """Chunks matching `where`, in the order every roll-up takes its top
    points from (the stored summary and the public view)."""
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
    return session.exec(
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
        .where(where)
        .where(SHOWN_CHUNKS)  # no price claims (HIDDEN_TOPICS)
        .order_by(
            YoutubeChannel.evidence_tier.asc().nullslast(),  # type: ignore[union-attr]
            LaptopReviewChunk.created_at.desc(),  # type: ignore[attr-defined]
            LaptopReviewChunk.id,
        )
    ).all()


def aggregate_for_laptop(laptop_id: uuid.UUID, session: Session) -> LaptopReviewSummary:
    """
    Roll up all processed chunks for a laptop into laptop_review_summary.
    Deduplicates by selecting the top _MAX_PER_SENTIMENT distinct chunk texts per sentiment.
    Creates a new summary row if one doesn't exist; updates in-place otherwise.
    """
    chunks = _ordered_chunks(session, LaptopReviewChunk.laptop_id == laptop_id)

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


def laptop_reviews(session: Session, laptop: Laptop) -> dict:
    """The public details-page view (GET /laptops/{id}/reviews, Review B3),
    computed from chunks on every request: no stored summary to go stale and
    no manual aggregate step. laptop_review_summary stays for admin use.

    Display rule, tiers never mixed:
      1. this laptop's own chunks plus its family's family-scope chunks
      2. if there are none, its sibling configurations' chunks, each naming
         the configuration the reviewer tested
      3. otherwise nothing -- the page says "no reviews yet"
    There is no query here, so unlike get_review_evidence the fallback is
    decided by existence, not relevance.
    """
    C = LaptopReviewChunk
    own = C.laptop_id == laptop.id
    source = "this_laptop"
    if laptop.family_id is None:
        chunks = _ordered_chunks(session, own)
    else:
        chunks = _ordered_chunks(session, or_(
            own, (C.family_id == laptop.family_id) & (C.scope == ChunkScope.FAMILY.value)
        ))
        if not chunks:
            chunks = _ordered_chunks(
                session, (C.family_id == laptop.family_id) & (C.laptop_id != laptop.id)
            )
            source = "other_configurations"
    if not chunks:
        return {"laptop_id": laptop.id, "source": "none", "review_count": 0,
                "strengths": [], "weaknesses": []}

    titles = dict(session.exec(
        select(RawYoutubeReview.video_id, RawYoutubeReview.video_title).where(
            RawYoutubeReview.video_id.in_({c.video_id for c in chunks})  # type: ignore[attr-defined]
        )
    ).all())
    names = dict(session.exec(
        select(Laptop.id, Laptop.product_name).where(
            Laptop.id.in_({c.laptop_id for c in chunks if c.laptop_id})  # type: ignore[attr-defined]
        )
    ).all())

    def point(c: LaptopReviewChunk) -> dict:
        if c.laptop_id == laptop.id:
            applies_to = "this_configuration"
        elif c.laptop_id is None:
            applies_to = "configuration_not_stated"
        else:
            applies_to = "other_configuration"
        other = applies_to == "other_configuration"
        return {
            "text": c.chunk_text,
            "sentiment": c.sentiment_tag,
            "applies_to": applies_to,
            "tested_on_laptop_id": c.laptop_id if other else None,
            "tested_on_name": names.get(c.laptop_id) if other else None,
            "channel_name": c.channel_name,
            "video_id": c.video_id,
            "video_title": titles.get(c.video_id),
            "timestamp_start_seconds": c.timestamp_start_seconds,
            "url": f"https://youtube.com/watch?v={c.video_id}&t={c.timestamp_start_seconds}",
        }

    def top(sentiment: str) -> list[dict]:
        out, seen = [], set()
        for c in chunks:
            key = c.chunk_text.strip().casefold()
            if c.sentiment_tag != sentiment or key in seen:
                continue
            seen.add(key)
            out.append(point(c))
            if len(out) == _MAX_PER_SENTIMENT:
                break
        return out

    return {
        "laptop_id": laptop.id,
        "source": source,
        "review_count": len({c.video_id for c in chunks}),
        "strengths": top("strength"),
        "weaknesses": top("weakness"),
    }
