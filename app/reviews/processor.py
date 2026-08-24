import time
import uuid
from typing import Literal

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import settings
from app.embeddings.service import embed_text
from app.logger import get_logger
from app.reviews.models import LaptopReviewChunk, RawYoutubeReview

logger = get_logger(__name__)

_CHUNK_DURATION_SECONDS = 45
_INTER_REQUEST_DELAY = 4  # seconds between Gemini calls

# Same model as the agent (app/agent/graph.py AGENT_MODEL) — kept as a local
# constant so the review pipeline doesn't import the agent stack.
_CHUNK_MODEL = "gemma-4-31b-it"


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


def process_raw_review(raw_review_id: uuid.UUID, session: Session) -> dict:
    """
    Chunk, summarise, sentiment-tag, and embed all transcript segments for a matched review.

    Returns a per-chunk outcome report, not just a count:

        {"chunks_total", "chunks_saved", "chunks_failed", "failures": [...]}

    A bare count hid partial processing — a review that saved 3 of 40 chunks
    and a review that saved 40 of 40 both looked like a success to
    /reviews/process-bulk, and the reason each chunk failed was only ever
    visible in the log file. `failures` carries the timestamp window and the
    exception class per chunk so a caller can tell "the model rejected this
    excerpt" apart from "we are being rate limited".

    Raises ValueError if the review is not in 'matched' status.
    """
    raw: RawYoutubeReview | None = session.exec(
        select(RawYoutubeReview).where(RawYoutubeReview.id == raw_review_id)
    ).first()

    if not raw:
        raise ValueError(f"RawYoutubeReview {raw_review_id} not found.")
    if raw.status != "matched" or raw.matched_laptop_id is None:
        raise ValueError(
            f"Review {raw_review_id} is not matched (status={raw.status}). "
            "Match it to a laptop first."
        )

    transcript_segments: list[dict] = raw.raw_transcript.get("segments", [])
    if not transcript_segments:
        raise ValueError(f"Review {raw_review_id} has no transcript segments stored.")

    chunks = _chunk_transcript(transcript_segments)
    if not chunks:
        return {"chunks_total": 0, "chunks_saved": 0, "chunks_failed": 0, "failures": []}

    llm = ChatGoogleGenerativeAI(
        model=_CHUNK_MODEL,
        temperature=0,
        google_api_key=settings.gemini_api_key,
    )
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
    laptop_id = raw.matched_laptop_id
    video_id = raw.video_id

    saved = 0
    failures: list[dict] = []

    for index, chunk in enumerate(chunks):
        try:
            analysis: _ChunkAnalysis = chain.invoke({"text": chunk["text"]})  # type: ignore[assignment]
            embedding = embed_text(analysis.summary)

            session.add(
                LaptopReviewChunk(
                    laptop_id=laptop_id,
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
        "Processed %d/%d chunks for video %s (%d failed)",
        saved, len(chunks), video_id, len(failures),
    )
    return {
        "chunks_total": len(chunks),
        "chunks_saved": saved,
        "chunks_failed": len(failures),
        "failures": failures,
    }
