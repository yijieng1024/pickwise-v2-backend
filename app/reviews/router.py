import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.common.pagination_service import (
    Page,
    PaginationParams,
    count_total,
    paginate,
)
from app.common.search_service import apply_search, search_query
from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.logger import get_logger
from app.reviews.aggregator import aggregate_for_laptop
from app.reviews.discovery import resolve_channel_from_url
from app.reviews.models import (
    LaptopReviewChunk,
    LaptopReviewSummary,
    ManualMatchRequest,
    RawYoutubeReview,
    RawYoutubeReviewRead,
    ReviewStatus,
    YoutubeChannel,
    YoutubeChannelCreate,
    YoutubeChannelUpdate,
)
from app.reviews.matcher import match_laptop
from app.reviews.processor import process_raw_review
from app.reviews.service import ingest_bulk, ingest_for_laptop
from app.reviews.transcript import (
    TERMINAL_FAILURES,
    TranscriptFailure,
    fetch_transcript,
)
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
        evidence_tier=body.evidence_tier,
        market_relevance=body.market_relevance,
        review_language=body.review_language,
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
    fetch_transcripts: bool = Query(
        default=True,
        description="Set false to record discovered videos without fetching "
        "transcripts — see POST /reviews/ingest-bulk.",
    ),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Trigger full discovery + transcript + matching pipeline for one laptop."""
    try:
        counts = ingest_for_laptop(
            laptop_id, session, fetch_transcripts=fetch_transcripts
        )
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
    fetch_transcripts: bool = Query(
        default=True,
        description=(
            "Set false for a discovery-only run: search YouTube and record the "
            "videos, but do not fetch transcripts. Discovery is metered by the "
            "Data API quota; the transcript endpoint is unmetered but "
            "independently rate-limited, and this path has no per-fetch delay, "
            "so a wide run would blow through a residential IP's limit and "
            "record fictional ip_blocked failures. Deferred rows land in "
            "`pending` with transcript_attempts=0 and are picked up later by "
            "POST /reviews/retry-transcripts."
        ),
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
        return ingest_bulk(
            session,
            limit=limit,
            skip_covered=skip_covered,
            fetch_transcripts=fetch_transcripts,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- Raw review management ---

def _to_read(
    review: RawYoutubeReview, laptop_name: str | None
) -> RawYoutubeReviewRead:
    """Read model for one raw review, with its matched laptop's name folded in."""
    return RawYoutubeReviewRead(
        id=review.id,
        video_id=review.video_id,
        channel_id=review.channel_id,
        video_title=review.video_title,
        published_at=review.published_at,
        matched_laptop_id=review.matched_laptop_id,
        matched_laptop_name=laptop_name,
        match_confidence=review.match_confidence,
        status=review.status,
        created_at=review.created_at,
    )


@router.get("/raw", response_model=Page[RawYoutubeReviewRead])
def list_raw_reviews(
    status: str | None = Query(
        default=None, description="Filter by status: pending | matched | rejected"
    ),
    search: str | None = search_query("Matches video title"),
    pagination: PaginationParams = Depends(),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """
    Paginated browse over ingested videos.

    Returns a `Page` envelope rather than a bare array because the admin Match
    Queue needs the filtered total to size its pager. It previously returned
    every row unbounded and the table sliced it client-side, so opening the
    screen downloaded the entire review table.
    """
    statement = select(RawYoutubeReview)
    if status:
        statement = statement.where(RawYoutubeReview.status == status)
    statement = apply_search(statement, search, [RawYoutubeReview.video_title])

    total = count_total(session, statement)

    # A deterministic order is required here, not cosmetic: with no ORDER BY,
    # Postgres is free to return rows in a different order per query, so the
    # same row can show up on two pages or on none. `id` breaks ties between
    # rows sharing a created_at.
    statement = statement.order_by(
        RawYoutubeReview.created_at.desc(),  # type: ignore[attr-defined]
        RawYoutubeReview.id,
    )
    reviews = session.exec(paginate(statement, pagination)).all()

    # One lookup for the whole page rather than a query per row. Only the
    # matched ids on this page are fetched, and only two columns of them.
    laptop_ids = {r.matched_laptop_id for r in reviews if r.matched_laptop_id}
    names: dict[uuid.UUID, str] = {}
    if laptop_ids:
        rows = session.exec(
            select(Laptop.id, Laptop.product_name).where(
                Laptop.id.in_(laptop_ids)  # type: ignore[attr-defined]
            )
        ).all()
        names = {laptop_id: product_name for laptop_id, product_name in rows}

    items = [
        _to_read(r, names.get(r.matched_laptop_id) if r.matched_laptop_id else None)
        for r in reviews
    ]
    return Page(
        items=items, total=total, skip=pagination.skip, limit=pagination.limit
    )


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
    review.status = ReviewStatus.MATCHED.value
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
        select(RawYoutubeReview).where(
            RawYoutubeReview.status == ReviewStatus.PENDING.value
        )
    ).all()

    updated = 0
    for review in pending:
        laptop_id, confidence = match_laptop(review.video_title, session)
        if laptop_id:
            review.matched_laptop_id = laptop_id
            review.match_confidence = confidence
            review.status = ReviewStatus.MATCHED.value
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
    """Run chunking, sentiment tagging, and embedding for a matched raw review.

    The response carries per-chunk outcomes, not just a saved count: chunk
    failures are individually recoverable and a partially-processed review
    still counts as "processed" to /process-bulk (existing chunks are the
    marker), so the caller has to be able to see the gap.
    """
    try:
        report = process_raw_review(review_id, session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return report


@router.post("/process-bulk")
def process_bulk(
    limit: int = Query(
        default=5,
        ge=1,
        le=50,
        description=(
            "Max reviews to process this run. Each transcript chunk costs one "
            "Gemini call + one embedding call with a 4s inter-request delay, "
            "so a single review can take a minute or more."
        ),
    ),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """
    Bulk chunking/embedding: runs the /process/{review_id} step over every
    matched raw review that has no chunks yet. process_raw_review doesn't flip
    the review's status, so existing laptop_review_chunks rows are the
    "already processed" marker — which also makes re-runs safe from writing
    duplicate chunks. Failures are reported per review and don't stop the run.
    """
    processed_video_ids = set(
        session.exec(select(LaptopReviewChunk.video_id).distinct()).all()
    )
    candidates = [
        r
        for r in session.exec(
            select(RawYoutubeReview).where(
                RawYoutubeReview.status == ReviewStatus.MATCHED.value
            )
        ).all()
        if r.video_id not in processed_video_ids
    ][:limit]

    results = []
    chunks_total = 0
    # `failed` counts reviews that raised outright; `partial` counts reviews
    # that returned but lost chunks. Before per-chunk outcomes existed the
    # second group was indistinguishable from a clean run.
    chunks_failed = 0
    failed = 0
    partial = 0
    for review in candidates:
        try:
            report = process_raw_review(review.id, session)
            chunks_total += report["chunks_saved"]
            chunks_failed += report["chunks_failed"]
            if report["chunks_failed"]:
                partial += 1
            results.append({
                "review_id": str(review.id),
                "video_title": review.video_title,
                **report,
            })
        except Exception as e:
            failed += 1
            logger.exception("Bulk processing failed for review %s", review.id)
            results.append({
                "review_id": str(review.id),
                "video_title": review.video_title,
                "error": str(e),
            })

    return {
        "candidates": len(candidates),
        "processed": len(candidates) - failed,
        "failed": failed,
        "partially_processed": partial,
        "chunks_saved": chunks_total,
        "chunks_failed": chunks_failed,
        "results": results,
    }


# --- Aggregation ---

# Declared before /summaries/{laptop_id} so "pending" is not parsed as a UUID.
@router.get("/summaries/pending")
def list_pending_summaries(
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """
    Laptops whose roll-up is missing or out of date.

    `state` is:
      - `new`     chunks exist but nothing has ever been aggregated
      - `stale`   a chunk arrived after the last aggregation
      - `current` the summary already covers every chunk

    Only `new` and `stale` are returned, because they are the work list.
    Re-running aggregation on a `current` laptop is safe but pointless.
    """
    chunk_stats = (
        select(
            LaptopReviewChunk.laptop_id.label("laptop_id"),  # type: ignore[attr-defined]
            func.count(LaptopReviewChunk.id).label("chunk_count"),
            func.max(LaptopReviewChunk.created_at).label("latest_chunk_at"),
        )
        .group_by(LaptopReviewChunk.laptop_id)
        .subquery()
    )

    rows = session.execute(
        select(
            chunk_stats.c.laptop_id,
            chunk_stats.c.chunk_count,
            chunk_stats.c.latest_chunk_at,
            Laptop.product_name,
            Laptop.model_code,
            LaptopReviewSummary.review_count,
            LaptopReviewSummary.last_updated_at,
        )
        .join(Laptop, Laptop.id == chunk_stats.c.laptop_id)  # type: ignore[arg-type]
        .outerjoin(
            LaptopReviewSummary,
            LaptopReviewSummary.laptop_id == chunk_stats.c.laptop_id,  # type: ignore[arg-type]
        )
        .order_by(chunk_stats.c.latest_chunk_at.desc())
    ).all()

    pending = []
    for row in rows:
        if row.last_updated_at is None:
            state = "new"
        elif row.latest_chunk_at is not None and row.latest_chunk_at > row.last_updated_at:
            state = "stale"
        else:
            continue  # already current

        pending.append(
            {
                "laptop_id": row.laptop_id,
                "product_name": row.product_name,
                "model_code": row.model_code,
                "chunk_count": row.chunk_count,
                "summary_review_count": row.review_count or 0,
                "last_aggregated_at": row.last_updated_at,
                "state": state,
            }
        )

    return {"total": len(pending), "items": pending}


@router.get("/summaries/{laptop_id}")
def get_laptop_summary(
    laptop_id: uuid.UUID,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """
    Read the stored roll-up without recomputing it.

    POST /aggregate/{laptop_id} returns the same shape but performs a write,
    so previewing what the chatbot currently quotes used to cost an
    aggregation run.
    """
    summary = session.exec(
        select(LaptopReviewSummary).where(LaptopReviewSummary.laptop_id == laptop_id)
    ).first()
    if not summary:
        raise HTTPException(
            status_code=404,
            detail="This laptop has not been aggregated yet.",
        )

    return {
        "laptop_id": summary.laptop_id,
        "review_count": summary.review_count,
        "strengths": summary.aggregated_strengths,
        "weaknesses": summary.aggregated_weaknesses,
        "last_updated_at": summary.last_updated_at,
    }


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

@router.post("/retry-transcripts")
def retry_transcripts(
    limit: int = Query(default=20, ge=1, le=100),
    delay_seconds: float = Query(
        default=2.0,
        ge=0.0,
        le=30.0,
        description="Pause between transcript fetches. See the rate-limit note "
        "in the endpoint description — this is not optional politeness.",
    ),
    max_consecutive_blocks: int = Query(
        default=3,
        ge=1,
        le=20,
        description="Abort the run after this many consecutive ip_blocked "
        "results instead of pushing through the remaining rows.",
    ),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """
    Re-fetch transcripts for rejected reviews whose failure was operational
    rather than a genuine caption gap.

    **Zero YouTube Data API quota cost is not zero rate limit.** The transcript
    endpoint is unmetered by the Data API — which is why this is separate from
    ingest, where a retry would first pay 100 units per channel for a search it
    does not need — but it is independently throttled by YouTube, and
    conflating "free" with "unlimited" is what produced the IP blocks in the
    first place. A residential IP starts getting blocked at roughly 30 fetches,
    and requests made *inside* the block window extend it, so a run that pushes
    through a block makes the situation worse and mislabels every remaining row
    as ip_blocked on the way. Hence both guards below: a delay between fetches,
    and a circuit breaker that stops after `max_consecutive_blocks`.

    Consecutive, not cumulative: an isolated block among successes is noise,
    while three in a row is the throttle engaging.

    Rows with failure_reason NULL are included: they predate the field, so
    their reason was erased by the old blanket exception handler and we
    cannot assume they were terminal.

    Also picks up rows that were never attempted at all (status=pending,
    transcript_attempts=0), which is what a discovery-only ingest run
    produces. Those rows are not failures; they are deferred work, and this is
    the only endpoint that can reach them — a later ingest skips any existing
    non-rejected row.
    """
    terminal = {f.value for f in TERMINAL_FAILURES}

    candidates = session.exec(
        select(RawYoutubeReview)
        .where(
            or_(
                # Rejected for an operational reason — the original case.
                and_(
                    RawYoutubeReview.status == ReviewStatus.REJECTED.value,
                    or_(
                        RawYoutubeReview.failure_reason.is_(None),      # type: ignore[union-attr]
                        RawYoutubeReview.failure_reason.notin_(terminal),  # type: ignore[union-attr]
                    ),
                ),
                # Never attempted at all — a discovery-only ingest run
                # (fetch_transcripts=false) records these with
                # transcript_attempts=0. Without this clause they would be
                # stranded forever: a later ingest skips any existing
                # non-rejected row, so nothing else would ever fetch them.
                #
                # The empty-transcript test is NOT redundant with
                # transcript_attempts=0. Migration 8e429682f918 added that
                # column with server_default '0', so all 28 rows that predate
                # it read as "never attempted" while already holding a full
                # transcript. Counting attempts alone would re-fetch every one
                # of them and spend the rate-limit budget this endpoint exists
                # to protect. What actually matters is whether the row has
                # segments, so ask that directly; the attempts test then only
                # keeps us off rows something else is already retrying.
                and_(
                    RawYoutubeReview.status == ReviewStatus.PENDING.value,
                    RawYoutubeReview.transcript_attempts == 0,
                    func.coalesce(
                        func.jsonb_array_length(
                            RawYoutubeReview.raw_transcript["segments"]  # type: ignore[index]
                        ),
                        0,
                    )
                    == 0,
                ),
            )
        )
        .order_by(RawYoutubeReview.created_at, RawYoutubeReview.id)  # type: ignore[arg-type]
        .limit(limit)
    ).all()

    recovered, still_failing = 0, {}
    attempted = 0
    consecutive_blocks = 0
    aborted = False

    for index, review in enumerate(candidates):
        if index > 0 and delay_seconds:
            time.sleep(delay_seconds)

        result = fetch_transcript(review.video_id)
        review.transcript_attempts += 1
        attempted += 1

        if result.ok:
            review.raw_transcript = {"segments": result.segments}
            review.transcript_language = result.language_code
            review.failure_reason = None
            # deliberately NOT MATCHED: a recovered transcript says nothing
            # about which laptop the video is about.
            review.status = ReviewStatus.PENDING.value
            recovered += 1
            consecutive_blocks = 0
        else:
            review.failure_reason = result.failure.value
            still_failing[result.failure.value] = (
                still_failing.get(result.failure.value, 0) + 1
            )
            if result.failure == TranscriptFailure.IP_BLOCKED:
                consecutive_blocks += 1
            else:
                consecutive_blocks = 0
        session.add(review)

        if consecutive_blocks >= max_consecutive_blocks:
            aborted = True
            logger.warning(
                "retry-transcripts aborted after %d consecutive ip_blocked "
                "results (%d of %d rows attempted)",
                consecutive_blocks, attempted, len(candidates),
            )
            break

    # Commit whatever was attempted. The rows we did reach have a real,
    # updated failure_reason and attempt count; throwing that away because the
    # run ended early would mean re-fetching them next time, which is exactly
    # the traffic the breaker exists to avoid.
    session.commit()
    return {
        "candidates": len(candidates),
        "attempted": attempted,
        "recovered": recovered,
        "still_failing": still_failing,
        # Explicit partial-run marker: without it a caller cannot tell an
        # abort from a run that simply had few candidates.
        "aborted_on_rate_limit": aborted,
        "not_attempted": len(candidates) - attempted,
    }
