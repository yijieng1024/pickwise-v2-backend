from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)


def _build_api() -> YouTubeTranscriptApi:
    """Direct connection by default; Webshare rotating-residential proxy when
    credentials are configured. YouTube blocks the transcript endpoint for
    datacenter IPs, so the proxy is required for this to work on Render."""
    if settings.webshare_proxy_username and settings.webshare_proxy_password:
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=settings.webshare_proxy_username,
                proxy_password=settings.webshare_proxy_password,
            )
        )
    return YouTubeTranscriptApi()


def fetch_transcript(video_id: str) -> list[dict] | None:
    """
    Fetch the transcript for a YouTube video.
    Returns a list of segments: [{"text": str, "start": float, "duration": float}, ...]
    Returns None if no transcript is available (auto-captions disabled or private video).
    No YouTube API quota cost — uses the public transcript endpoint directly.
    """
    try:
        ytt = _build_api()
        fetched = ytt.fetch(video_id)
        segments = [{"text": s.text, "start": s.start, "duration": s.duration} for s in fetched]
        logger.info("Transcript fetched for video %s (%d segments)", video_id, len(segments))
        return segments
    except Exception as e:
        logger.info("No transcript available for video %s: %s", video_id, e)
        return None
