"""
The review funnel: where each review is, and why it stopped.

One classification, used by both GET /reviews/pipeline-status (the counts) and
GET /reviews/raw?step= / ?stuck= (the list each count opens), so a number and
the list behind it cannot disagree.

Every non-irrelevant review lands in exactly one place: it reached some step
and is either stuck there for one reason, or reached the last step.

    found -> transcript -> linked -> processed -> shown

  found       every review not dismissed as irrelevant
  transcript  has transcript segments
  linked      matched, with at least one link (what processing needs)
  processed   no window left to process and not marked for re-processing
  shown       at least one claim visible on the site (SHOWN_CHUNKS)

Plus coverage: which active laptops show reviews, by the same tiers as
aggregator.laptop_reviews().
"""
import uuid

from sqlalchemy import exists, func, or_
from sqlmodel import Session, select

from app.laptops.laptop_models import Laptop, LaptopStatus
from app.reviews.link_model import ReviewLaptopLink
from app.reviews.models import SHOWN_CHUNKS, LaptopReviewChunk, RawYoutubeReview, ReviewStatus
from app.reviews.processor import processing_candidates
from app.reviews.transcript import TERMINAL_FAILURES

STEPS = ("found", "transcript", "linked", "processed", "shown")

# Stuck reasons, keyed by the step a review failed to reach.
STUCK = {
    "transcript": ("transcript_terminal", "transcript_retryable", "transcript_deferred", "transcript_unknown"),
    "linked": ("unlinked",),
    "processed": ("never_processed", "partial", "reprocess_requested"),
    "shown": ("no_shown_claims",),
}
STUCK_KEYS = tuple(k for keys in STUCK.values() for k in keys)

_TERMINAL = {f.value for f in TERMINAL_FAILURES}


def _transcript_stuck(failure_reason, attempts: int, status: str) -> str:
    if failure_reason in _TERMINAL:
        return "transcript_terminal"
    if failure_reason is not None:
        return "transcript_retryable"
    if attempts == 0 and status == ReviewStatus.PENDING.value:
        # A discovery-only ingest run: never fetched, not failed.
        return "transcript_deferred"
    # NULL reason on a fetched row: predates failure_reason, cause erased.
    return "transcript_unknown"


def classify(session: Session) -> dict[uuid.UUID, tuple[str, str | None]]:
    """review id -> (furthest step reached, stuck reason or None)."""
    has_segments = func.coalesce(
        func.jsonb_array_length(RawYoutubeReview.raw_transcript["segments"]),  # type: ignore[index]
        0,
    ) > 0
    has_link = exists().where(ReviewLaptopLink.raw_review_id == RawYoutubeReview.id)
    rows = session.exec(
        select(
            RawYoutubeReview.id,
            RawYoutubeReview.video_id,
            RawYoutubeReview.status,
            RawYoutubeReview.failure_reason,
            RawYoutubeReview.transcript_attempts,
            has_segments,
            has_link,
        ).where(RawYoutubeReview.status != ReviewStatus.IRRELEVANT.value)
    ).all()

    # The same function /reviews/process-bulk runs.
    candidates = {c["review_id"]: c for c in processing_candidates(session)}
    shown_videos = set(
        session.exec(select(LaptopReviewChunk.video_id).where(SHOWN_CHUNKS).distinct()).all()
    )

    out: dict[uuid.UUID, tuple[str, str | None]] = {}
    for rid, video_id, status, failure, attempts, segments, linked in rows:
        if not segments:
            out[rid] = ("found", _transcript_stuck(failure, attempts, status))
        elif not (linked and status == ReviewStatus.MATCHED.value):
            out[rid] = ("transcript", "unlinked")
        elif rid in candidates:
            c = candidates[rid]
            if c["reprocess"]:
                reason = "reprocess_requested"
            elif c["windows_saved"] == 0:
                reason = "never_processed"
            else:
                reason = "partial"
            out[rid] = ("linked", reason)
        elif video_id not in shown_videos:
            out[rid] = ("processed", "no_shown_claims")
        else:
            out[rid] = ("shown", None)
    return out


def funnel_counts(classified: dict[uuid.UUID, tuple[str, str | None]]) -> list[dict]:
    """Per step: how many reviews reached it, and how many stopped just short
    of it, by reason. count(previous step) = count(step) + sum(stuck)."""
    reached = {s: 0 for s in STEPS}
    stuck = {k: 0 for k in STUCK_KEYS}
    for step, reason in classified.values():
        for s in STEPS[: STEPS.index(step) + 1]:
            reached[s] += 1
        if reason:
            stuck[reason] += 1
    return [
        {"step": step, "count": reached[step], "stuck": {k: stuck[k] for k in STUCK.get(step, ())}}
        for step in STEPS
    ]


def review_ids(session: Session, step: str | None = None, stuck: str | None = None) -> list[uuid.UUID]:
    """The reviews behind one funnel number, for GET /reviews/raw. Both
    filters given = both must hold."""
    floor = STEPS.index(step) if step is not None else 0
    return [
        rid for rid, (reached, reason) in classify(session).items()
        if STEPS.index(reached) >= floor and (stuck is None or reason == stuck)
    ]


# --- Coverage ----------------------------------------------------------------

def shows_reviews(laptop=Laptop):
    """SQL: this laptop's details page shows at least one review claim.

    aggregator.laptop_reviews() picks tier 1 (own chunks + the family's
    family-scope chunks) and falls back to tier 2 (siblings' chunks); the
    tiers decide WHICH claims are shown, not whether any are. The union of
    the two is "any shown chunk on this laptop, or anywhere in its family"."""
    C = LaptopReviewChunk
    return exists().where(
        SHOWN_CHUNKS,
        or_(
            C.laptop_id == laptop.id,
            (laptop.family_id.is_not(None)) & (C.family_id == laptop.family_id),  # type: ignore[union-attr]
        ),
    )


def coverage(session: Session) -> dict:
    active = Laptop.status == LaptopStatus.ACTIVE.value
    active_laptops, with_reviews = session.exec(
        select(func.count(Laptop.id), func.count(Laptop.id).filter(shows_reviews())).where(active)
    ).one()
    families_with_active, families_with_reviews = session.exec(
        select(
            func.count(func.distinct(Laptop.family_id)),
            func.count(func.distinct(Laptop.family_id)).filter(shows_reviews()),
        ).where(active, Laptop.family_id.is_not(None))  # type: ignore[union-attr]
    ).one()
    return {
        "families_with_active": families_with_active,
        "families_with_reviews": families_with_reviews,
        "active_laptops": active_laptops,
        "active_laptops_with_reviews": with_reviews,
    }
