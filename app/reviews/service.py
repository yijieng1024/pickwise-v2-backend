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
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.logger import get_logger
from app.reviews.discovery import discover_videos
from app.reviews.matcher import match_laptop
from app.reviews.models import RawYoutubeReview, YoutubeChannel
from app.reviews.transcript import fetch_transcript

logger = get_logger(__name__)


def _family_group_key(laptop: Laptop) -> str:
    """Which YouTube search this laptop belongs to.

    Prefers `family_id` over `family_key(product_name)`. The key is only a
    *seed* — family_key.py says so in its own docstring: it over-merges ASUS
    and Apple (model year lives inside the first paren and is discarded) and
    over-splits Acer (model code lives outside any paren, so two SKUs of one
    machine keep two keys). `laptop_family` is the human-corrected version of
    that same grouping, so wherever it exists it is strictly better — and an
    over-split key costs real money here, 100 quota units per channel for a
    search we already ran under the other key.

    A null family_id means "not grouped yet", which is a valid state, not an
    error: fall back to the seed key rather than guessing a group. The two
    namespaces are prefixed so a family UUID can never collide with a key.
    """
    if laptop.family_id:
        return f"family:{laptop.family_id}"
    return f"key:{family_key(laptop.product_name)}"


def _pick_representative(laptops: list[Laptop]) -> Laptop:
    """One configuration stands in for the whole family in the YouTube query.

    Shortest product_name first, then name, then id. Deterministic is the
    requirement — `setdefault` over an unordered `select(Laptop)` meant the
    search string for a family changed between runs with no data change, which
    makes ingest results irreproducible and any quota accounting a guess.

    Shortest-first is the tiebreak worth having rather than an arbitrary one:
    the query is built from the full product_name, so the shortest member
    carries the fewest configuration tokens ("ROG Zephyrus G14" rather than
    "ROG Zephyrus G14 (2025, GA403WW) 32GB 1TB"), and configuration tokens are
    exactly what a reviewer's title does not contain.
    """
    return min(laptops, key=lambda l: (len(l.product_name), l.product_name, str(l.id)))


def ingest_bulk(session: Session, limit: int = 5, skip_covered: bool = True) -> dict:
    """
    Run the discovery + transcript + match pipeline across the catalog, one
    search per laptop *family* (family_id where the laptop has been grouped,
    family_key as the seed otherwise — see _family_group_key). Discovered
    videos are matched against the whole catalog by the matcher, so one family
    search can populate raw reviews for several variants.

    Only `active` laptops are searched: retired products are not worth 100
    quota units per channel, and reviews should not be attached to them.

    skip_covered=True skips families that already have at least one matched
    raw review, so repeated runs walk through the catalog day by day within
    the YouTube quota (cost ≈ active_channels × 100 units per family). See
    the note at the `covered` computation for why "matched" and not "has any
    raw review" — it is a schema limit, not a preference.
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

    # Active laptops only, consistent with retrieve_candidates,
    # conversation_laptops, graph.py::_pool_block and get_ranking_for_use_case.
    #
    # The justification is correctness and quota, NOT match quality: we should
    # not spend 100 units per channel discovering reviews for a product that
    # has been retired, nor attach reviews to one. It was measured, and it does
    # not improve matching — restricting candidates to the 238 active rows makes
    # ties slightly WORSE (76 -> 78 tied videos, 26 -> 27 cross-family), because
    # suspended rows share match keys with active siblings, so removing them
    # shortens ties without dissolving them, while removing a row that was the
    # lone rank-1 winner creates a fresh tie among the runners-up. Do not cite
    # this change as a matching improvement.
    laptops = session.exec(
        select(Laptop).where(Laptop.status == LaptopStatus.ACTIVE.value)
    ).all()

    grouped: dict[str, list[Laptop]] = {}
    for laptop in laptops:
        grouped.setdefault(_family_group_key(laptop), []).append(laptop)
    families: dict[str, Laptop] = {
        key: _pick_representative(members) for key, members in grouped.items()
    }

    covered: set[str] = set()
    if skip_covered:
        # NOTE: coverage can only be derived through matched_laptop_id, which
        # is the single link from a review back to a laptop. A `pending` review
        # has that column NULL by construction (match_laptop returns None below
        # MATCH_THRESHOLD), so a family whose videos are all in the human queue
        # still reads as uncovered and gets re-searched at full quota cost.
        # Fixing that needs somewhere to record which family a search covered —
        # see ADR-0012. Do not paper over it by re-running the matcher here:
        # 89% of titles tie at rank 1, so quota decisions would be made from
        # database row order.
        matched_laptops = session.exec(
            select(Laptop)
            .join(RawYoutubeReview, RawYoutubeReview.matched_laptop_id == Laptop.id)  # type: ignore[arg-type]
        ).all()
        # Same grouping function as `families` above — two different key
        # derivations would silently never intersect, and skip_covered would
        # quietly become a no-op.
        covered = {_family_group_key(l) for l in matched_laptops}

    # Sorted, because `limit` slices this list: with dict-insertion order
    # inherited from an unordered SELECT, two runs would pick two different
    # families to spend the day's quota on, and "families_remaining" would not
    # describe a walk through the catalog at all.
    todo = sorted(
        ((key, laptop) for key, laptop in families.items() if key not in covered),
        key=lambda item: (item[1].product_name, str(item[1].id)),
    )[:limit]

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


def _parse_published(value: str | None) -> datetime | None:
    """YouTube returns RFC3339 with a literal 'Z'; fromisoformat wants +00:00."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ingest_for_laptop(laptop_id: uuid.UUID, session: Session) -> dict:
    """
    Run the full discovery + matching + transcript-fetch pipeline for one laptop.
    Stages:
      1. Discover videos across all active channels
      2. Skip videos already ingested (unless previously rejected)
      3. Fuzzy-match the video title against the laptop catalog
      4. Fetch the transcript
      5. Persist to raw_youtube_reviews (status: matched | pending | rejected)

    Matching runs BEFORE the transcript fetch so that every row — including
    rejected ones — carries a match_confidence. Previously a video with no
    transcript was never matched at all, leaving a NULL confidence that made
    the row invisible to any triage sorted by confidence.

    The transcript is fetched regardless of match outcome: with the current
    matcher most videos land in `pending` and are matched by a human later,
    so skipping their transcripts would leave process_raw_review with nothing
    to work on.

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

    counts = {
        "discovered": 0, "skipped": 0,
        "matched": 0, "pending": 0, "rejected": 0,
    }
    if not channels:
        return counts

    videos = discover_videos(brand_name, laptop.product_name, channels)
    counts["discovered"] = len(videos)
    logger.info("Discovered %d videos for laptop %s", len(videos), laptop.product_name)

    # Guards against a duplicate video_id inside one discovery batch: the
    # `existing` lookup below cannot see rows added but not yet committed in
    # this same transaction, and video_id is unique — so a duplicate would
    # only surface as an IntegrityError at commit, losing the whole batch.
    seen: set[str] = set()

    for video in videos:
        video_id = video["video_id"]
        if video_id in seen:
            counts["skipped"] += 1
            continue
        seen.add(video_id)

        existing = session.exec(
            select(RawYoutubeReview).where(RawYoutubeReview.video_id == video_id)
        ).first()
        # Skip already-processed videos; retry rejected ones — their failure
        # may have been operational (IP block, timeout) rather than a real
        # caption gap.
        if existing and existing.status != "rejected":
            counts["skipped"] += 1
            continue

        matched_laptop_id, confidence = match_laptop(video["video_title"], session)
        result = fetch_transcript(video_id)

        if result.ok:
            status = "matched" if matched_laptop_id else "pending"
            raw_transcript = {"segments": result.segments}
            failure_reason = None
            language = result.language_code
        else:
            status = "rejected"
            raw_transcript = {}
            failure_reason = result.failure.value
            language = None

        if existing:
            existing.video_title = video["video_title"]
            existing.raw_transcript = raw_transcript
            existing.matched_laptop_id = matched_laptop_id
            existing.match_confidence = confidence
            existing.status = status
            existing.failure_reason = failure_reason
            existing.transcript_language = language
            existing.transcript_attempts += 1
            session.add(existing)
        else:
            session.add(
                RawYoutubeReview(
                    video_id=video_id,
                    channel_id=video["channel_id"],
                    video_title=video["video_title"],
                    published_at=_parse_published(video.get("published_at")),
                    raw_transcript=raw_transcript,
                    matched_laptop_id=matched_laptop_id,
                    match_confidence=confidence,
                    status=status,
                    failure_reason=failure_reason,
                    transcript_language=language,
                    transcript_attempts=1,
                )
            )

        counts[status] += 1

    session.commit()
    logger.info("Ingest complete for laptop %s: %s", laptop.product_name, counts)
    return counts