import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.common.pagination_service import (
    Page,
    PaginationParams,
    count_total,
    paginate,
)
from app.common.search_service import apply_search, search_query
from app.database import get_session
from app.laptops.brand_model import LaptopBrand
from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.logger import get_logger
from app.reviews.aggregator import aggregate_for_laptop
from app.reviews.config_evidence import scan_config_evidence
from app.reviews.discovery import fetch_descriptions, resolve_channel_from_url
from app.reviews.models import (
    LaptopReviewChunk,
    LaptopReviewSummary,
    ManualMatchRequest,
    RawYoutubeReview,
    RawYoutubeReviewRead,
    ReviewIrrelevantRequest,
    ReviewStatus,
    YoutubeChannel,
    YoutubeChannelCreate,
    YoutubeChannelUpdate,
)
from app.reviews.link_model import ReviewLaptopLink, ReviewLinkCreate
from app.reviews.link_service import (
    column_label,
    config_row,
    create_human_link,
    differing_columns,
    family_members,
    links_for_reviews,
    mark_indistinguishable,
    resolve_family_for_laptop,
    separability,
)
from app.reviews.matcher import match_laptop
from app.reviews.processor import process_raw_review
from app.reviews.service import family_worklist, ingest_bulk, ingest_for_laptop
from app.reviews.transcript import (
    TERMINAL_FAILURES,
    TranscriptFetchGuard,
)
from app.users.auth import get_current_admin

# YouTube Data API search.list costs 100 units per call, and the free daily
# quota is 10,000. Named here rather than inlined so the pipeline screen and
# the ingest estimate cannot disagree about the arithmetic.
_QUOTA_UNITS_PER_SEARCH = 100
_DAILY_QUOTA_UNITS = 10_000

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
        default=None,
        description="Filter by status: pending | matched | rejected | irrelevant",
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
    """Manually pair a raw review to a product line, and optionally to a config.

    Cut over to review_laptop_link (ADR-0012). This was the last write path
    still recording a human decision as match_confidence = 100.0, which is
    indistinguishable from a perfect fuzzy score — the exact ambiguity
    match_source exists to remove — and the links it made were invisible to
    GET /reviews/pending. Two write paths disagreeing about where the truth
    lives is worse than either alone.

    Accepts family_id, laptop_id, or both. family_id alone is the "tested
    configuration unknown" case the links endpoint already supports; when only
    laptop_id is given the family is resolved from it, which keeps existing
    callers working unchanged.

    Every guard is shared with POST /reviews/{id}/links via
    link_service.create_human_link, so both paths return identical 400/404/409
    responses and cannot drift apart.
    """
    review = session.get(RawYoutubeReview, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    family_id = body.family_id or resolve_family_for_laptop(session, body.laptop_id)  # type: ignore[arg-type]
    link = create_human_link(session, review_id, family_id, body.laptop_id)

    # matched_laptop_id is still written, and is NOT a competing source of
    # truth: the link is the answer, and this column is a denormalised cache of
    # it for the one reader not yet cut over — process_raw_review, whose chunk
    # path is a separate pass. Do not add readers of it; read the links.
    #
    # A family-only match leaves it NULL, because there is no configuration to
    # cache. Status then stays as it is rather than going to MATCHED: in this
    # pipeline MATCHED means "ready for chunk processing", and process_raw_review
    # needs a laptop_id, so promoting it would only queue a review that
    # /process-bulk picks up and fails on every run. The link records that a
    # human has acted; the queue shows it with its link attached.
    review.matched_laptop_id = body.laptop_id
    if body.laptop_id is not None:
        # No longer 100.0 — see the module docstring on link_model. The human
        # decision lives in the link's match_source; a fabricated score here
        # would resurrect the ambiguity in the cached column.
        review.match_confidence = None
        review.status = ReviewStatus.MATCHED.value
    session.add(review)
    session.commit()
    session.refresh(review)
    session.refresh(link)

    logger.info(
        "Review %s manually matched to family %s (config %s)",
        review_id, family_id, body.laptop_id,
    )
    return {
        "review": _to_read(review, None),
        "links": links_for_reviews(session, [review_id]).get(review_id, []),
    }


@router.patch("/raw/{review_id}/irrelevant")
def set_review_irrelevant(
    review_id: uuid.UUID,
    body: ReviewIrrelevantRequest | None = None,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Dismiss a queue item that is not about a laptop, or undo that dismissal.

    Discovery no longer requires the word "review" in a title — Chinese
    channels do not title in English, so the keyword cost more recall than it
    bought precision — and the price of that is non-laptop videos reaching the
    queue. This is how they leave it.

    The row is marked, never deleted. video_id is UNIQUE and
    service.should_skip_existing leaves any non-REJECTED row untouched, so a
    deleted row is rediscovered and reinserted on the very next ingest run and
    lands back in the queue; the row is what makes the dismissal stick.

    Reversible, and only exactly reversible between PENDING and IRRELEVANT —
    which is why the transition is refused (409) from any other status. Undo
    has one destination, PENDING, so allowing a MATCHED or REJECTED row in
    would mean restoring it as PENDING and silently discarding a match or a
    transcript-failure verdict. A mis-click on the queue must cost nothing.
    """
    # An omitted body means the default, `irrelevant: true` — the dismiss
    # button sends no payload. A shared module-level default instance would be
    # one mutable object handed to every request, so build a fresh one.
    body = body or ReviewIrrelevantRequest()

    review = session.get(RawYoutubeReview, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    target = (
        ReviewStatus.IRRELEVANT.value if body.irrelevant else ReviewStatus.PENDING.value
    )
    allowed_from = (
        ReviewStatus.PENDING.value if body.irrelevant else ReviewStatus.IRRELEVANT.value
    )
    if review.status != allowed_from and review.status != target:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Review is {review.status!r}; only a {allowed_from!r} review can "
                f"become {target!r}. This transition is deliberately restricted to "
                "pending <-> irrelevant so that undo is exact."
            ),
        )

    # Idempotent: already in the target state is success, not a conflict. The
    # dismiss button will be double-clicked.
    if review.status != target:
        review.status = target
        session.add(review)
        session.commit()
        session.refresh(review)
        logger.info("Review %s status set to %s", review_id, target)

    return _to_read(review, None)


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
                #
                # The general trap, because it will recur with every future
                # additive column: a server_default backfill makes historical
                # rows indistinguishable from newly-created ones. The default
                # value is a fact about the migration, not about the row, so a
                # predicate that treats it as a real observation will silently
                # sweep in the entire pre-migration table. Test the thing you
                # actually mean — here, "has no transcript".
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

    # The delay and the breaker live in TranscriptFetchGuard, shared with the
    # ingest pipeline — see app/reviews/transcript.py. Two copies of a
    # rate-limit guard is how one of them silently stops matching the other.
    guard = TranscriptFetchGuard(
        delay_seconds=delay_seconds,
        max_consecutive_blocks=max_consecutive_blocks,
    )
    recovered, still_failing = 0, {}

    for review in candidates:
        result = guard.fetch(review.video_id)
        review.transcript_attempts += 1

        if result.ok:
            review.raw_transcript = {"segments": result.segments}
            review.transcript_language = result.language_code
            review.failure_reason = None
            # deliberately NOT MATCHED: a recovered transcript says nothing
            # about which laptop the video is about.
            review.status = ReviewStatus.PENDING.value
            recovered += 1
        else:
            review.failure_reason = result.failure.value
            still_failing[result.failure.value] = (
                still_failing.get(result.failure.value, 0) + 1
            )
        session.add(review)

        if guard.tripped:
            break

    # Commit whatever was attempted. The rows we did reach have a real,
    # updated failure_reason and attempt count; throwing that away because the
    # run ended early would mean re-fetching them next time, which is exactly
    # the traffic the breaker exists to avoid.
    session.commit()
    return {
        "candidates": len(candidates),
        "attempted": guard.attempted,
        "recovered": recovered,
        "still_failing": still_failing,
        # Explicit partial-run marker: without it a caller cannot tell an
        # abort from a run that simply had few candidates.
        "aborted_on_rate_limit": guard.tripped,
        "not_attempted": len(candidates) - guard.attempted,
    }


# --- Human review-linking screen (ADR-0012) ---------------------------------
#
# These four endpoints back the admin screen that turns a `pending` review into
# one or more review_laptop_link rows. The flow is: pick the review from the
# queue, search for a family, then optionally pick the exact configuration —
# family first because that is the level a reviewer actually covers, and the
# configuration is frequently unknowable from the video.

@router.get("/pending")
def list_pending_reviews(
    pagination: PaginationParams = Depends(),
    search: str | None = search_query("Matches video title"),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """The human match queue: reviews awaiting a laptop link.

    Returns existing links alongside each review rather than making the screen
    fetch them per row — a review can already hold links (from the backfill, or
    from a partly-finished linking session) and the queue has to show that
    without an N+1.
    """
    # Explicit columns, and NOT the whole row: raw_transcript is a large JSONB
    # blob (hundreds of segments per video) and the queue loads every pending
    # row, so selecting the model would pull megabytes to compute one integer.
    # jsonb_array_length does it in the database and ships back an int.
    segment_count = func.coalesce(
        func.jsonb_array_length(RawYoutubeReview.raw_transcript["segments"]),  # type: ignore[index]
        0,
    ).label("segment_count")

    statement = select(
        RawYoutubeReview.id,
        RawYoutubeReview.video_id,
        RawYoutubeReview.video_title,
        RawYoutubeReview.channel_id,
        RawYoutubeReview.published_at,
        RawYoutubeReview.status,
        RawYoutubeReview.match_confidence,
        RawYoutubeReview.created_at,
        segment_count,
    # PENDING only, which is what excludes dismissed rows: an IRRELEVANT
    # review is not "pending with a flag set", it has left the queue. Reach it
    # through GET /reviews/raw?status=irrelevant — that is the undo surface.
    ).where(RawYoutubeReview.status == ReviewStatus.PENDING.value)
    statement = apply_search(statement, search, [RawYoutubeReview.video_title])
    total = count_total(session, statement)

    # created_at then id: video_title is not unique and created_at ties for
    # rows written in one ingest batch, so without the id a row could appear on
    # two pages of the queue or on none.
    statement = statement.order_by(
        RawYoutubeReview.created_at.desc(),  # type: ignore[attr-defined]
        RawYoutubeReview.id,
    )
    reviews = list(session.exec(paginate(statement, pagination)).all())

    channel_names = dict(
        session.execute(
            sa_select(YoutubeChannel.channel_id, YoutubeChannel.channel_name).where(
                YoutubeChannel.channel_id.in_(  # type: ignore[attr-defined]
                    {r.channel_id for r in reviews}
                )
            )
        ).all()
    ) if reviews else {}

    links = links_for_reviews(session, [r.id for r in reviews])

    items = [
        {
            "id": r.id,
            "video_id": r.video_id,
            "video_url": f"https://www.youtube.com/watch?v={r.video_id}",
            "video_title": r.video_title,
            "channel_id": r.channel_id,
            "channel_name": channel_names.get(r.channel_id),
            "published_at": r.published_at,
            "status": r.status,
            "match_confidence": r.match_confidence,
            # A review with no transcript can be linked perfectly and still
            # produce zero chunks, so the time spent linking it is wasted. The
            # count lets the queue show that before the human commits to a row.
            "segment_count": r.segment_count,
            "has_transcript": r.segment_count > 0,
            "links": links.get(r.id, []),
        }
        for r in reviews
    ]
    return Page(
        items=items, total=total, skip=pagination.skip, limit=pagination.limit
    )


@router.get("/families")
def search_families(
    q: str | None = Query(default=None, description="Substring of the family name."),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Family search — step one of the linking flow.

    Returns the family plus its brand and member count. The count is what tells
    the human whether picking a configuration is even a question: a one-member
    family has nothing to disambiguate.
    """
    statement = sa_select(
        LaptopFamily.id,
        LaptopFamily.name,
        LaptopFamily.is_verified,
        LaptopBrand.name,
        func.count(Laptop.id),
    ).join(
        LaptopBrand, LaptopBrand.id == LaptopFamily.brand_id
    ).join(
        Laptop, Laptop.family_id == LaptopFamily.id, isouter=True
    ).group_by(
        LaptopFamily.id, LaptopFamily.name, LaptopFamily.is_verified, LaptopBrand.name
    )
    if q:
        statement = statement.where(LaptopFamily.name.ilike(f"%{q}%"))  # type: ignore[attr-defined]
    # name then id — family names are not unique, so the id keeps paging and
    # repeat searches stable.
    statement = statement.order_by(LaptopFamily.name, LaptopFamily.id).limit(limit)

    return [
        {
            "family_id": fid,
            "name": name,
            "brand": brand,
            "is_verified": verified,
            "member_count": count,
        }
        for fid, name, verified, brand, count in session.execute(statement).all()
    ]


@router.post("/backfill-descriptions")
def backfill_descriptions(
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Fill video_description on rows ingested before the column existed.

    The configuration-evidence scan is only as good as its source material, and
    the description is the richest of the two — a channel that pastes a spec
    table into it answers the configuration question outright, which is
    especially common on the Chinese-language channels. Every row already in
    the table predates the column, so without this the feature would be blind
    on the entire existing queue and only work on newly ingested videos.

    Costs 1 quota unit per 50 videos (videos.list), against 100 per channel for
    a discovery search — the whole table is a couple of units. That is why this
    is a plain synchronous endpoint and not a background job.

    Only touches rows where video_description IS NULL, so it is safe to re-run
    and never overwrites a description already held. A video whose lookup fails
    or which has been deleted from YouTube stays NULL and is picked up by the
    next run; a video with a genuinely empty description is stored as "" and is
    not retried, which is the distinction the nullable column exists to keep.
    """
    candidates = session.exec(
        select(RawYoutubeReview)
        .where(RawYoutubeReview.video_description.is_(None))  # type: ignore[union-attr]
        .order_by(RawYoutubeReview.created_at, RawYoutubeReview.id)  # type: ignore[arg-type]
        .limit(limit)
    ).all()
    if not candidates:
        return {"candidates": 0, "filled": 0, "still_missing": 0, "quota_units": 0}

    descriptions = fetch_descriptions([r.video_id for r in candidates])

    filled = 0
    for review in candidates:
        text = descriptions.get(review.video_id)
        if text is None:
            continue
        review.video_description = text
        session.add(review)
        filled += 1
    session.commit()

    remaining = session.exec(
        select(func.count(RawYoutubeReview.id)).where(
            RawYoutubeReview.video_description.is_(None)  # type: ignore[union-attr]
        )
    ).one()
    logger.info(
        "Backfilled %d of %d video descriptions (%d still missing)",
        filled, len(candidates), remaining,
    )
    return {
        "candidates": len(candidates),
        "filled": filled,
        # Not an error count: a video deleted from YouTube can never be filled,
        # and will show up here on every run.
        "not_returned": len(candidates) - filled,
        "still_missing": remaining,
        "quota_units": -(-len(candidates) // 50),
    }


@router.get("/families/{family_id}/configs")
def list_family_configs(
    family_id: uuid.UUID,
    review_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Scan this review's description and transcript for spec strings "
            "belonging to the family's members, and return the matches."
        ),
    ),
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """What the human needs to answer "which configuration was tested?" — or to
    be told that the question has no answer.

    Three things this returns, in the order the screen should trust them.

    **`separable`** comes first because it can cancel the question entirely. A
    family whose members differ only in RAM and storage cannot be told apart
    from any review — see `link_service.separability`. When it is false the
    screen must show `separability_reason` and render no chooser at all: four
    options where the discriminating information cannot exist invites a guess,
    and a guessed laptop_id is worse than a null one, because null is honest
    and a guess attaches this video's claims to a machine nobody tested.

    **`evidence`** (only with `review_id`) is what turns the step from
    investigation into confirmation. The title never carries a spec — "The
    First Panther Lake Laptop I Strongly Recommend" names no CPU, GPU or RAM —
    so without this the only way to answer honestly is to watch the video:
    minutes per review on a screen budgeted for ten seconds. `evidence.hits`
    carries each match with its surrounding words and, for transcript hits, the
    second it was said at. `evidence.found_nothing` is a real answer: the video
    does not say, stop looking.

    **`configs`** is the table, and it is computed over the members that remain
    after suspended rows are dropped. That ordering matters: `differing_columns`
    is recomputed on the survivors, so a column that only looked discriminating
    because of a placeholder row disappears. On the ExpertBook Ultra family,
    removing the suspended RM 0 row collapses price to a constant 11999 and
    price stops being offered as a distinguishing column.
    """
    family = session.get(LaptopFamily, family_id)
    if not family:
        raise HTTPException(status_code=404, detail="Family not found.")

    # Suspended only. `inactive` stays visible on purpose: a delisted laptop is
    # the normal subject of an old review, and hiding it would make those
    # reviews unlinkable. See family_members.
    all_members = family_members(session, family_id)
    members = family_members(
        session, family_id, exclude_statuses=(LaptopStatus.SUSPENDED.value,)
    )
    # Recomputed over the filtered set, never the full one — that is the whole
    # reason the two lists exist separately here.
    columns = differing_columns(members)
    separable, separability_code, reason = separability(columns, len(members))
    # The filter can empty a family outright — ExpertBook P3 G2 is two rows and
    # both are suspended. Name the cause rather than letting the screen report
    # an empty catalog for a product line that is plainly in it.
    if not members and all_members:
        reason = (
            f"Every configuration of this product line ({len(all_members)}) is "
            "suspended in the catalog, so there is nothing to link to. Leave "
            "the configuration unset."
        )

    evidence = None
    if review_id is not None:
        review = session.get(RawYoutubeReview, review_id)
        if not review:
            raise HTTPException(status_code=404, detail="Review not found.")
        evidence = scan_config_evidence(
            description=review.video_description,
            transcript_segments=(review.raw_transcript or {}).get("segments"),
            members=members,
            columns=columns,
            label_for=column_label,
        )

    configs = [config_row(laptop, columns) for laptop in members]
    indistinguishable = mark_indistinguishable(configs, columns)

    return {
        "family_id": family.id,
        "name": family.name,
        "is_verified": family.is_verified,
        "member_count": len(members),
        # How many rows were withheld, and why. Never silently: a row vanishing
        # from a list the admin saw yesterday needs an explanation on the page,
        # not in a commit message.
        "excluded_suspended": len(all_members) - len(members),
        "columns": [
            {"key": column, "label": column_label(column)} for column in columns
        ],
        # Empty when the family has one member, or when its members differ in
        # none of the tracked spec columns — both mean "there is nothing to
        # choose between", which is a useful answer, not an error.
        "identical": not columns and len(members) > 1,
        # False means: render no chooser, show the reason, leave selection null.
        "separable": separable,
        # Branch on the code, not the prose: single_config is "nothing to choose
        # between" and a screen may still offer the one row, while
        # ram_storage_only means the question has no answer at all.
        "separability_code": separability_code,
        "separability_reason": reason,
        "evidence": evidence,
        # Rows identical to another row in every shown column. Flagged, not
        # merged or hidden — see mark_indistinguishable. A count here lets the
        # screen say the pick between them is arbitrary rather than leaving the
        # human to discover it by staring at two identical rows.
        "indistinguishable_count": indistinguishable,
        "configs": configs,
    }


@router.post("/{raw_review_id}/links", status_code=201)
def create_review_link(
    raw_review_id: uuid.UUID,
    body: ReviewLinkCreate,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Link a review to a family, and optionally to a specific configuration.

    Callable several times for one review: a comparison video covers several
    machines, and that is the case this whole table exists for.

    Every link created here is match_source=HUMAN with match_confidence NULL. A
    human did not score anything — recording a fabricated 100.0 is what made
    human decisions indistinguishable from perfect fuzzy matches in the column
    this table replaces.
    """
    review = session.get(RawYoutubeReview, raw_review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    # Shared with PATCH /reviews/raw/{id}/match — see link_service.
    link = create_human_link(session, raw_review_id, body.family_id, body.laptop_id)
    session.commit()
    session.refresh(link)

    logger.info(
        "Review %s linked to family %s (config %s) by a human",
        raw_review_id, body.family_id, body.laptop_id,
    )
    # Returns every link on the review, not just the new one: the screen shows
    # a review's full link set and would otherwise have to re-fetch the queue.
    return links_for_reviews(session, [raw_review_id]).get(raw_review_id, [])


@router.delete("/links/{link_id}", status_code=204)
def delete_review_link(
    link_id: uuid.UUID,
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Remove one link. Undo for the screen.

    Deletes any link, auto or human: an auto link from the backfill is exactly
    the kind of thing a reviewer needs to be able to remove. It does not touch
    raw_youtube_reviews.matched_laptop_id, which is still the column
    process_raw_review reads — the two are not yet cut over.
    """
    link = session.get(ReviewLaptopLink, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="Link not found.")
    session.delete(link)
    session.commit()
    return None


@router.get("/pipeline-status")
def pipeline_status(
    session: Session = Depends(get_session),
    _: None = Depends(get_current_admin),
):
    """Queue depth for every stage of the review pipeline. Costs no quota.

    The pipeline screen previously asked for a batch size with no idea what was
    waiting: "Max families" against an unknown remaining count, "Max reviews"
    against an unknown candidate count, and an aggregate action that made the
    admin search the catalog by name for laptops it could have listed. Each
    number below is computed by the SAME code path as the run that consumes it,
    which is the only way a status number is worth showing — a count derived
    differently from the action it describes is worse than no count.

    Deliberately read-only: no YouTube search, no transcript fetch, no Gemini
    call. Safe to poll on page load.
    """
    families, covered = family_worklist(session, skip_covered=True)
    active_channels = session.exec(
        select(func.count(YoutubeChannel.id)).where(
            YoutubeChannel.active == True  # noqa: E712
        )
    ).one()

    # Same candidate rule as /reviews/process-bulk: matched reviews that have
    # no chunks yet. Existing chunks are the "already processed" marker, since
    # processing never flips the review's status.
    processed_video_ids = set(
        session.exec(select(LaptopReviewChunk.video_id).distinct()).all()
    )
    matched_video_ids = session.exec(
        select(RawYoutubeReview.video_id).where(
            RawYoutubeReview.status == ReviewStatus.MATCHED.value
        )
    ).all()
    process_candidates = sum(
        1 for v in matched_video_ids if v not in processed_video_ids
    )

    # Link queue: pending reviews, split by whether a human has linked them.
    # Done-ness comes from links, never from status — a family-only link leaves
    # the review `pending` on purpose (the chunk path still needs a laptop_id).
    pending_ids = session.exec(
        select(RawYoutubeReview.id).where(
            RawYoutubeReview.status == ReviewStatus.PENDING.value
        )
    ).all()
    linked_ids = set(
        session.exec(
            select(ReviewLaptopLink.raw_review_id).distinct()
        ).all()
    )
    pending_linked = sum(1 for r in pending_ids if r in linked_ids)

    # Dismissed as not-a-laptop. Reported next to the queue it was removed from
    # because the ratio is the number that matters, not the count: it measures
    # what dropping the "review" keyword from discovery actually cost. Roughly
    # 10-15% of everything ingested says the recall was worth it; 40% says
    # discovery is too loose and wants a `laptop`/`notebook` term added —
    # putting `review` back would reintroduce the original problem, that
    # Chinese channels do not title in English.
    irrelevant_total = session.exec(
        select(func.count(RawYoutubeReview.id)).where(
            RawYoutubeReview.status == ReviewStatus.IRRELEVANT.value
        )
    ).one()
    reviews_total = session.exec(select(func.count(RawYoutubeReview.id))).one()

    summaries = list_pending_summaries(session=session, _=None)

    remaining = len([k for k in families if k not in covered])
    return {
        "ingest": {
            "families_total": len(families),
            "families_covered": len(covered & families.keys()),
            "families_remaining": remaining,
            "active_channels": active_channels,
            # What one family costs. The screen multiplies this by the batch
            # size so the admin sees the spend before clicking, not after: at
            # 19 channels a default batch of 5 is 9,500 of the 10,000 daily cap.
            "quota_units_per_family": active_channels * _QUOTA_UNITS_PER_SEARCH,
            "daily_quota_units": _DAILY_QUOTA_UNITS,
        },
        "link": {
            "pending_total": len(pending_ids),
            "pending_linked": pending_linked,
            "pending_unlinked": len(pending_ids) - pending_linked,
            "irrelevant_total": irrelevant_total,
            "reviews_total": reviews_total,
            "irrelevant_ratio": (
                round(irrelevant_total / reviews_total, 4) if reviews_total else 0.0
            ),
        },
        "process": {"candidates": process_candidates},
        "aggregate": {
            "pending_total": summaries["total"],
            "new": sum(1 for s in summaries["items"] if s["state"] == "new"),
            "stale": sum(1 for s in summaries["items"] if s["state"] == "stale"),
        },
    }
