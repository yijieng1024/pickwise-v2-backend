from youtube_transcript_api import YouTubeTranscriptApi

from app.logger import get_logger

logger = get_logger(__name__)


def fetch_transcript(video_id: str) -> list[dict] | None:
    """
    Fetch the transcript for a YouTube video.
    Returns a list of segments: [{"text": str, "start": float, "duration": float}, ...]
    Returns None if no transcript is available (auto-captions disabled or private video).
    No YouTube API quota cost — uses the public transcript endpoint directly.
    """
    try:
        ytt = YouTubeTranscriptApi()
        fetched = ytt.fetch(video_id)
        segments = [{"text": s.text, "start": s.start, "duration": s.duration} for s in fetched]
        logger.info("Transcript fetched for video %s (%d segments)", video_id, len(segments))
        return segments
    except Exception as e:
        logger.info("No transcript available for video %s: %s", video_id, e)
        return None
