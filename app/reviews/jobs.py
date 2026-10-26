"""
Background-job workers for the review pipeline: ingest, retry transcripts,
process (one review or a batch) and rematch.

Each `*_worker` returns a `worker(session, progress)` for `run_job`, which gives
it one Session and records every `progress.advance()` on a short session of
its own. Per-item failures become the job's `errors` (`item` = review id, or
`family:<key>` for ingest, which works per family before review rows exist);
the job's `result` holds `done` / `failed` / `skipped` plus the report the
synchronous endpoint used to return.

No transaction may stay open across a YouTube, transcript, Gemini or embedding
call (the QueuePool lesson): ingest and processing already commit before every
slow call, and retry reads plain values, commits, then writes each row in a
short transaction after its fetch.
"""
import uuid
from typing import Callable, Optional

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.common.job_service import JobProgress
from app.logger import get_logger
from app.reviews.link_service import human_linked_review_ids, write_auto_link
from app.reviews.matcher import match_laptop
from app.reviews.models import RawYoutubeReview, ReviewStatus
from app.reviews.processor import AlreadyProcessed, process_raw_review, processing_candidates
from app.reviews.service import ingest_bulk
from app.reviews.transcript import TERMINAL_FAILURES, TranscriptFetchGuard

logger = get_logger(__name__)

Worker = Callable[[Session, JobProgress], dict]


# --- Ingest -----------------------------------------------------------------

def ingest_bulk_worker(limit: int, skip_covered: bool, fetch_transcripts: bool) -> Worker:
    def worker(session: Session, progress: JobProgress) -> dict:
        report = ingest_bulk(
            session, limit=limit, skip_covered=skip_covered,
            fetch_transcripts=fetch_transcripts, progress=progress,
        )
        failed = sum(1 for r in report["results"] if "error" in r)
        return {
            "done": len(report["results"]) - failed,
            "failed": failed,
            # Families not reached: the run stopped on a rate-limit trip or a cancel.
            "skipped": report["families_attempted"] - len(report["results"]),
            **report,
        }
    return worker


# --- Retry transcripts -------------------------------------------------------

def retry_candidates_query(limit: Optional[int] = None):
    """Reviews a transcript retry would fetch: rejected for a non-terminal
    reason (or one erased before failure_reason existed), plus rows a
    discovery-only ingest left with no attempt and no transcript.

    The empty-transcript test is NOT redundant with transcript_attempts=0:
    migration 8e429682f918 added that column with server_default '0', so rows
    that predate it read as "never attempted" while already holding a full
    transcript. Test the thing actually meant -- "has no transcript".
    """
    terminal = [f.value for f in TERMINAL_FAILURES]
    statement = (
        select(RawYoutubeReview.id, RawYoutubeReview.video_id)
        .where(
            or_(
                and_(
                    RawYoutubeReview.status == ReviewStatus.REJECTED.value,
                    or_(
                        RawYoutubeReview.failure_reason.is_(None),  # type: ignore[union-attr]
                        RawYoutubeReview.failure_reason.notin_(terminal),  # type: ignore[union-attr]
                    ),
                ),
                and_(
                    RawYoutubeReview.status == ReviewStatus.PENDING.value,
                    RawYoutubeReview.transcript_attempts == 0,
                    func.coalesce(
                        func.jsonb_array_length(
                            RawYoutubeReview.raw_transcript["segments"]  # type: ignore[index]
                        ),
                        0,
                    ) == 0,
                ),
            )
        )
        .order_by(RawYoutubeReview.created_at, RawYoutubeReview.id)  # type: ignore[arg-type]
    )
    return statement.limit(limit) if limit else statement


def retry_transcripts_worker(
    limit: int, delay_seconds: float, max_consecutive_blocks: int
) -> Worker:
    def worker(session: Session, progress: JobProgress) -> dict:
        candidates = [(rid, vid) for rid, vid in session.exec(retry_candidates_query(limit)).all()]
        # Plain values, then end the read transaction before the first fetch.
        session.commit()
        progress.set_total(len(candidates))

        guard = TranscriptFetchGuard(
            delay_seconds=delay_seconds, max_consecutive_blocks=max_consecutive_blocks,
        )
        recovered, still_failing = 0, {}
        for review_id, video_id in candidates:
            result = guard.fetch(video_id)
            # A short write transaction after the fetch, committed per row so a
            # killed job keeps what it already fetched.
            review = session.get(RawYoutubeReview, review_id)
            if review is None:
                session.rollback()
                progress.advance(succeeded=False, item=str(review_id), error="review deleted during the run")
                continue
            review.transcript_attempts += 1
            if result.ok:
                review.raw_transcript = {"segments": result.segments}
                review.transcript_language = result.language_code
                review.failure_reason = None
                # Deliberately NOT matched: a recovered transcript says nothing
                # about which laptop the video is about.
                review.status = ReviewStatus.PENDING.value
                recovered += 1
            else:
                review.failure_reason = result.failure.value
                still_failing[result.failure.value] = still_failing.get(result.failure.value, 0) + 1
            session.add(review)
            session.commit()
            progress.advance(
                succeeded=result.ok, item=str(review_id),
                error=None if result.ok else result.failure.value,
            )
            if guard.tripped or progress.cancel_requested:
                break

        return {
            "done": recovered,
            "failed": sum(still_failing.values()),
            "skipped": len(candidates) - guard.attempted,
            "candidates": len(candidates),
            "attempted": guard.attempted,
            "recovered": recovered,
            "still_failing": still_failing,
            "aborted_on_rate_limit": guard.tripped,
            "not_attempted": len(candidates) - guard.attempted,
        }
    return worker


# --- Process -----------------------------------------------------------------

def _failure_message(report: dict) -> str:
    first = report["failures"][0] if report["failures"] else None
    detail = f": {first['error_type']}: {first['error']}" if first else ""
    return f"{report['chunks_failed']} of {report['chunks_total']} windows failed{detail}"


def _process(session: Session, progress: JobProgress, candidates: list[dict]) -> dict:
    """Process each candidate in turn. process_raw_review commits before every
    Gemini and embedding call, and after every window."""
    progress.set_total(len(candidates))
    done = failed = skipped = chunks_saved = chunks_failed = 0
    results = []
    for candidate in candidates:
        review_id = candidate["review_id"]
        row = {"review_id": str(review_id), "video_title": candidate["video_title"]}
        try:
            report = process_raw_review(review_id, session)
        except AlreadyProcessed as e:
            # Finished by someone else since the candidates were read.
            session.rollback()
            skipped += 1
            results.append({**row, "skipped": str(e)})
            progress.advance(succeeded=True, item=str(review_id))
        except Exception as e:
            session.rollback()
            failed += 1
            logger.exception("Processing failed for review %s", review_id)
            results.append({**row, "error": str(e)})
            progress.advance(succeeded=False, item=str(review_id), error=f"{type(e).__name__}: {e}")
        else:
            chunks_saved += report["chunks_saved"]
            chunks_failed += report["chunks_failed"]
            results.append({**row, **report})
            # A partial review is a failure: windows are missing from the site.
            if report["complete"]:
                done += 1
                progress.advance(succeeded=True, item=str(review_id))
            else:
                failed += 1
                progress.advance(succeeded=False, item=str(review_id), error=_failure_message(report))
        if progress.cancel_requested:
            break
    return {
        "done": done, "failed": failed, "skipped": skipped,
        "candidates": len(candidates),
        "chunks_saved": chunks_saved, "chunks_failed": chunks_failed,
        "results": results,
    }


def process_bulk_worker(limit: int) -> Worker:
    def worker(session: Session, progress: JobProgress) -> dict:
        candidates = processing_candidates(session)[:limit]
        session.commit()
        return _process(session, progress, candidates)
    return worker


def process_one_worker(review_id: uuid.UUID, video_title: str) -> Worker:
    def worker(session: Session, progress: JobProgress) -> dict:
        return _process(session, progress, [{"review_id": review_id, "video_title": video_title}])
    return worker


# --- Rematch -----------------------------------------------------------------

def rematch_worker() -> Worker:
    """Re-run auto-matching on pending reviews. Local fuzzy matching and
    database writes only, so one transaction is fine.

    A review with ANY human link is never touched -- not its links, not its
    status: a family-only human match leaves a review pending on purpose. A
    match is written as an auto link (replacing any earlier auto link); a
    matched laptop with no family cannot be linked and stays pending."""
    def worker(session: Session, progress: JobProgress) -> dict:
        pending = session.exec(
            select(RawYoutubeReview).where(RawYoutubeReview.status == ReviewStatus.PENDING.value)
        ).all()
        human = human_linked_review_ids(session)
        progress.set_total(len(pending))

        updated = skipped_human = ungrouped = unmatched = 0
        for review in pending:
            item = str(review.id)
            if review.id in human:
                skipped_human += 1
            else:
                laptop_id, confidence = match_laptop(review.video_title, session)
                if not laptop_id:
                    unmatched += 1
                elif not write_auto_link(session, review.id, laptop_id, confidence):
                    ungrouped += 1
                else:
                    review.status = ReviewStatus.MATCHED.value
                    session.add(review)
                    updated += 1
            progress.advance(succeeded=True, item=item)

        session.commit()
        logger.info(
            "Rematch: %d of %d pending reviews matched (%d human-linked skipped, %d ungrouped)",
            updated, len(pending), skipped_human, ungrouped,
        )
        return {
            "done": updated,
            "failed": 0,
            "skipped": skipped_human + ungrouped + unmatched,
            "pending_total": len(pending),
            "newly_matched": updated,
            "skipped_human_linked": skipped_human,
            "matched_laptop_has_no_family": ungrouped,
            "no_match": unmatched,
        }
    return worker
