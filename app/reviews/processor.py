import uuid
from datetime import datetime, timezone
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


def process_raw_review(raw_review_id: uuid.UUID, session: Session) -> int:
    """
    Chunk, summarise, sentiment-tag, and embed all transcript segments for a matched review.
    Returns the number of chunks written to laptop_review_chunks.
    Raises ValueError if the review is not in 'matched' status.
    """
    import time

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
        return 0

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

    saved = 0
    for chunk in chunks:
        try:
            analysis: _ChunkAnalysis = chain.invoke({"text": chunk["text"]})  # type: ignore[assignment]
            embedding = embed_text(analysis.summary)

            session.add(
                LaptopReviewChunk(
                    laptop_id=raw.matched_laptop_id,
                    video_id=raw.video_id,
                    channel_name=channel_name,
                    timestamp_start_seconds=chunk["start"],
                    timestamp_end_seconds=chunk["end"],
                    chunk_text=analysis.summary,
                    embedding=embedding,
                    sentiment_tag=analysis.sentiment_tag,
                )
            )
            saved += 1
            time.sleep(_INTER_REQUEST_DELAY)
        except Exception as e:
            logger.warning(
                "Chunk processing failed for video %s at %ds: %s",
                raw.video_id,
                chunk["start"],
                e,
            )

    session.commit()
    logger.info("Processed %d chunks for video %s", saved, raw.video_id)
    return saved
