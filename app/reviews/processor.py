import time
import uuid
from typing import Literal

from app.common import llm_config
from app.common.llm import build_llm
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel
from sqlmodel import Session, select

from app.common.llm_usage import LLMFeature
from app.embeddings.service import embed_text
from app.logger import get_logger
from app.reviews.link_service import links_by_review, processing_target
from app.reviews.models import ChunkScope, LaptopReviewChunk, RawYoutubeReview, ReviewStatus

logger = get_logger(__name__)

_CHUNK_DURATION_SECONDS = 45
_INTER_REQUEST_DELAY = 4  # seconds between Gemini calls



class _ChunkAnalysis(BaseModel):
    summary: str
    sentiment_tag: Literal["strength", "weakness", "neutral"]


_SYSTEM_PROMPT = (
    "You are a laptop product analyst. You will be given a short excerpt from a YouTube "
    "laptop review transcript (which may be in any language). Your task:\n"
    "1. Write a concise English paraphrase (1-2 sentences) of what the reviewer is saying. "
    "Do NOT reproduce the original transcript verbatim.\n"
    "2. Classify the sentiment: 'strength' (reviewer is positive about a feature), "
    "'weakness' (reviewer is critical), or 'neutral' (informational, no clear sentiment).\n"
    "Return only valid JSON with keys 'summary' and 'sentiment_tag'."
)

_HUMAN_PROMPT = "Transcript excerpt:\n{text}"


def _first_line(exc: Exception) -> str:
    """One-line exception text for a JSON payload. Several of the libraries in
    this path (google-genai, httpx) raise multi-line messages, and the raw
    string would make the /process-bulk response unreadable."""
    return str(exc).strip().split(chr(10))[0][:300]


def _chunk_transcript(
    transcript: list[dict], chunk_duration: int = _CHUNK_DURATION_SECONDS
) -> list[dict]:
    """Group raw transcript segments into ~chunk_duration-second windows."""
    chunks: list[dict] = []
    current: dict = {"texts": [], "start": None, "end": None}

    for seg in transcript:
        if current["start"] is None:
            current["start"] = int(seg["start"])
        current["texts"].append(seg["text"])
        current["end"] = int(seg["start"] + seg["duration"])

        if current["end"] - current["start"] >= chunk_duration:
            chunks.append(
                {
                    "text": " ".join(current["texts"]),
                    "start": current["start"],
                    "end": current["end"],
                }
            )
            current = {"texts": [], "start": None, "end": None}

    if current["texts"]:
        chunks.append(
            {
                "text": " ".join(current["texts"]),
                "start": current["start"],
                "end": current["end"],
            }
        )

    return chunks


class AlreadyProcessed(ValueError):
    """Every window of the transcript already has a chunk for this laptop.

    The duplicate guard. 11 of the 15 processed videos were processed twice
    before it existed (205 duplicate rows), because nothing stopped
    /process/{id} from running again on a finished review."""


def _saved_windows(
    session: Session, video_id: str, family_id: uuid.UUID, laptop_id: uuid.UUID | None
) -> set[int]:
    return set(
        session.exec(
            select(LaptopReviewChunk.timestamp_start_seconds)
            .where(LaptopReviewChunk.video_id == video_id)
            .where(LaptopReviewChunk.family_id == family_id)
            .where(
                LaptopReviewChunk.laptop_id.is_(None)  # type: ignore[union-attr]
                if laptop_id is None
                else LaptopReviewChunk.laptop_id == laptop_id
            )
        ).all()
    )


def processing_candidates(session: Session) -> list[dict]:
    """Reviews /process-bulk should run, and /pipeline-status counts -- one
    function so the two can never disagree.

    A candidate is MATCHED, has effective links that resolve to one target
    (link_service.processing_target: one family, at most one configuration),
    and has at least one transcript window with no chunk for that target yet. "Any chunk exists" is
    NOT "processed": a run that saved 3 of 40 windows would otherwise never be
    retried. Completion is derived from the transcript, so it needs no column.
    """
    reviews = session.exec(
        select(RawYoutubeReview)
        .where(RawYoutubeReview.status == ReviewStatus.MATCHED.value)
        .order_by(RawYoutubeReview.created_at, RawYoutubeReview.id)  # type: ignore[arg-type]
    ).all()
    links = links_by_review(session, [r.id for r in reviews])
    saved: dict[tuple, set[int]] = {}
    for video_id, family_id, laptop_id, start in session.exec(
        select(
            LaptopReviewChunk.video_id,
            LaptopReviewChunk.family_id,
            LaptopReviewChunk.laptop_id,
            LaptopReviewChunk.timestamp_start_seconds,
        )
    ).all():
        saved.setdefault((video_id, family_id, laptop_id), set()).add(start)

    out = []
    for r in reviews:
        target = processing_target(links[r.id])
        segments = (r.raw_transcript or {}).get("segments", [])
        if target is None or not segments:
            continue
        expected = {c["start"] for c in _chunk_transcript(segments)}
        done = saved.get((r.video_id, *target), set()) & expected
        if done != expected:
            out.append({
                "review_id": r.id,
                "video_title": r.video_title,
                "windows_expected": len(expected),
                "windows_saved": len(done),
            })
    return out


def process_raw_review(raw_review_id: uuid.UUID, session: Session) -> dict:
    """
    Chunk, summarise, sentiment-tag, and embed the transcript windows of a
    matched review that do not have a chunk yet.

    Returns a per-chunk outcome report, not just a count:

        {"windows_expected", "windows_already_saved", "chunks_total",
         "chunks_saved", "chunks_failed", "failures": [...], "complete"}

    `chunks_total` is the windows attempted THIS run. `complete` is whether
    every window now has a chunk; a partial run (complete=False) is picked up
    again by /process-bulk and only its missing windows are redone, so a retry
    never writes a duplicate. `failures` carries the window and the exception
    class per chunk, so "the model rejected this excerpt" and "we are being
    rate limited" stay distinguishable.

    The target comes from review_laptop_link (link_service.processing_target):
    the review must be MATCHED and its effective links must name one family and
    at most one configuration. A config link writes config-scope chunks on that
    laptop; a family-only link writes family-scope chunks with laptop_id NULL
    (Review B -- the scope is a rule here, Review C makes it a per-chunk call).

    Raises AlreadyProcessed (a ValueError) when there is nothing left to do,
    and ValueError when the review is not processable.
    """
    raw: RawYoutubeReview | None = session.exec(
        select(RawYoutubeReview).where(RawYoutubeReview.id == raw_review_id)
    ).first()

    if not raw:
        raise ValueError(f"RawYoutubeReview {raw_review_id} not found.")
    target = processing_target(links_by_review(session, [raw.id])[raw.id])
    if raw.status != ReviewStatus.MATCHED.value or target is None:
        raise ValueError(
            f"Review {raw_review_id} is not processable (status={raw.status}): it "
            "must be matched and linked to one family and at most one "
            "configuration. Reviews linked to several families wait for Review C."
        )
    family_id, laptop_id = target
    scope = (ChunkScope.FAMILY if laptop_id is None else ChunkScope.CONFIG).value

    transcript_segments: list[dict] = raw.raw_transcript.get("segments", [])
    if not transcript_segments:
        raise ValueError(f"Review {raw_review_id} has no transcript segments stored.")

    all_chunks = _chunk_transcript(transcript_segments)
    already = _saved_windows(session, raw.video_id, family_id, laptop_id)
    chunks = [c for c in all_chunks if c["start"] not in already]
    already_saved = len(all_chunks) - len(chunks)
    if not chunks:
        raise AlreadyProcessed(
            f"Review {raw_review_id} is already fully processed "
            f"({already_saved}/{len(all_chunks)} windows saved)."
        )

    # The `review_processor` use: configured primary, then each backup in order, on 429 / timeout
    # / 5xx. Resolved once per review, so one review is not split across a
    # mid-run settings change (a fallback can still answer single chunks).
    llm = build_llm(llm_config.REVIEW_PROCESSOR, temperature=0, feature=LLMFeature.REVIEW_CHUNK)
    structured_llm = llm.with_structured_output(_ChunkAnalysis)
    chain = (
        ChatPromptTemplate.from_messages(
            [("system", _SYSTEM_PROMPT), ("human", _HUMAN_PROMPT)]
        )
        | structured_llm
    )

    # Fetch channel name from youtube_channels via channel_id on raw review
    from app.reviews.models import YoutubeChannel

    channel = session.exec(
        select(YoutubeChannel).where(
            YoutubeChannel.channel_id == raw.channel_id
        )
    ).first()
    channel_name = channel.channel_name if channel else raw.channel_id

    # Read the columns we need BEFORE the loop. Each chunk now commits, and
    # session.commit() expires every loaded instance by default, so touching
    # `raw.video_id` inside the loop would silently re-SELECT the row once per
    # chunk — and would raise outright if the row were gone.
    video_id = raw.video_id
    # End the read transaction before the first Gemini + embedding call. The
    # per-chunk commit below only closes transactions from chunk 1 on; the
    # reads above otherwise held the connection through chunk 0's LLM call.
    session.commit()

    saved = 0
    failures: list[dict] = []

    for index, chunk in enumerate(chunks):
        try:
            analysis: _ChunkAnalysis = chain.invoke({"text": chunk["text"]})  # type: ignore[assignment]
            embedding = embed_text(analysis.summary)

            session.add(
                LaptopReviewChunk(
                    laptop_id=laptop_id,
                    family_id=family_id,
                    scope=scope,
                    video_id=video_id,
                    channel_name=channel_name,
                    timestamp_start_seconds=chunk["start"],
                    timestamp_end_seconds=chunk["end"],
                    chunk_text=analysis.summary,
                    embedding=embedding,
                    sentiment_tag=analysis.sentiment_tag,
                )
            )
            # Commit per chunk rather than once at the end. A 64-chunk review
            # spends 64 x 4s = four minutes in network I/O, and holding one
            # transaction open across all of it pins a Supabase pooler
            # connection for the whole run and throws away every completed
            # chunk if the last one fails. The commit costs nothing next to a
            # Gemini round trip, and partial progress is worth keeping: chunks
            # are the "already processed" marker /process-bulk reads.
            session.commit()
            saved += 1
        except Exception as e:
            # The failed chunk may have left the session dirty (a flush error
            # aborts the transaction), so roll back before the next iteration
            # or every subsequent commit fails with InFailedSqlTransaction.
            session.rollback()
            failures.append(
                {
                    "chunk_index": index,
                    "start_seconds": chunk["start"],
                    "end_seconds": chunk["end"],
                    "error_type": type(e).__name__,
                    "error": _first_line(e),
                }
            )
            logger.warning(
                "Chunk processing failed for video %s at %ds: %s: %s",
                video_id,
                chunk["start"],
                type(e).__name__,
                e,
            )
        finally:
            # In `finally`, not at the end of the `try`. The delay exists to
            # keep us under Gemini's free-tier rate limit, and the single most
            # likely reason a chunk fails IS a 429 — so skipping the delay on
            # failure meant the code responded to rate limiting by firing the
            # next request immediately. That turns one 429 into a cascade.
            # It runs after the last chunk too: /process-bulk moves straight
            # on to the next review, so there is always a next request.
            time.sleep(_INTER_REQUEST_DELAY)

    logger.info(
        "Processed %d/%d chunks for video %s (%d failed, %d already saved)",
        saved, len(chunks), video_id, len(failures), already_saved,
    )
    return {
        "windows_expected": len(all_chunks),
        "windows_already_saved": already_saved,
        "complete": already_saved + saved == len(all_chunks),
        "chunks_total": len(chunks),
        "chunks_saved": saved,
        "chunks_failed": len(failures),
        "failures": failures,
    }
