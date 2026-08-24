from dataclasses import dataclass
from enum import Enum
from typing import Optional

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig
# NOTE: verify these import paths against your installed version — in some
# v1.x releases they live under youtube_transcript_api._errors instead.
from youtube_transcript_api import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

_PROXY_WARNED = False

class TranscriptFailure(str, Enum):
    """Why a transcript fetch produced nothing.

    The split that matters is TERMINAL vs RETRYABLE. Terminal means no
    language preference, no proxy and no retry can ever recover this video —
    it is the ASR decision set. Everything else is an operational failure and
    must not be counted as a data gap. Collapsing these two into `None` is
    what hid the dominant cause of the reject bucket.
    """
    NO_TRACK = "no_track"                 # terminal
    VIDEO_UNAVAILABLE = "video_unavailable"  # terminal
    IP_BLOCKED = "ip_blocked"             # retryable
    NETWORK = "network_error"             # retryable
    UNKNOWN = "unknown"                   # retryable — unclassified


TERMINAL_FAILURES = {
    TranscriptFailure.NO_TRACK,
    TranscriptFailure.VIDEO_UNAVAILABLE,
}


@dataclass
class TranscriptResult:
    segments: Optional[list[dict]]
    failure: Optional[TranscriptFailure] = None
    language_code: Optional[str] = None
    is_generated: Optional[bool] = None

    @property
    def ok(self) -> bool:
        return self.segments is not None

    @property
    def retryable(self) -> bool:
        return self.failure is not None and self.failure not in TERMINAL_FAILURES

# Family prefixes in preference order, NOT literal language codes.
# `fetch()`'s default is the exact code ('en',), which is why a video listing
# zh-Hans / zh-Hant / en-US still raised NoTranscriptFound: 'en-US' != 'en'.
_LANG_FAMILIES = ("en", "zh")


def _pick_track(transcript_list):
    """Choose the best available caption track.

    Two orderings, applied in this order:
      1. language family preference (en, then zh, then anything)
      2. manually-created before auto-generated — ASR captions carry
         recognition errors, and product names are exactly what ASR gets
         wrong, which is what the matcher depends on.

    We deliberately do NOT call .translate() on a Chinese track. The chunk
    processor paraphrases downstream anyway, and translating at fetch time
    destroys the original wording irrecoverably. Store the source language
    and let the read side decide.
    """
    tracks = list(transcript_list)

    for family in _LANG_FAMILIES:
        for want_generated in (False, True):
            for t in tracks:
                if t.language_code.lower().split("-")[0] != family:
                    continue
                if t.is_generated is want_generated:
                    return t

    # Any track at all beats no transcript.
    for want_generated in (False, True):
        for t in tracks:
            if t.is_generated is want_generated:
                return t
    return None

def fetch_transcript(video_id: str) -> TranscriptResult:
    """No YouTube API quota cost — uses the public transcript endpoint."""
    try:
        ytt = _build_api()
        track = _pick_track(ytt.list(video_id))
        if track is None:
            return TranscriptResult(None, TranscriptFailure.NO_TRACK)

        fetched = track.fetch()
        segments = [
            {"text": s.text, "start": s.start, "duration": s.duration}
            for s in fetched
        ]
        logger.info(
            "Transcript fetched for %s (%d segments, lang=%s, generated=%s)",
            video_id, len(segments), track.language_code, track.is_generated,
        )
        return TranscriptResult(
            segments,
            language_code=track.language_code,
            is_generated=track.is_generated,
        )

    except (TranscriptsDisabled, NoTranscriptFound):
        return TranscriptResult(None, TranscriptFailure.NO_TRACK)
    except VideoUnavailable:
        return TranscriptResult(None, TranscriptFailure.VIDEO_UNAVAILABLE)
    except Exception as e:
        name = type(e).__name__
        # Class-name matching rather than importing every error type, because
        # these classes moved between library versions. Log the class name
        # unconditionally so an unclassified failure shows up as itself
        # instead of silently joining the UNKNOWN pile.
        if "Blocked" in name or "TooManyRequests" in name:
            failure = TranscriptFailure.IP_BLOCKED
        elif "Timeout" in name or "Connection" in name:
            failure = TranscriptFailure.NETWORK
        else:
            failure = TranscriptFailure.UNKNOWN
        # No exception-text field: youtube-transcript-api's messages start
        # with a newline and embed a multi-paragraph README block, so the old
        # first-line argument resolved to an empty string and every line ended
        # in a dangling arrow. The class name and the video id already locate
        # the problem exactly.
        logger.warning(
            "Transcript fetch failed for %s: %s (classified %s)",
            video_id, name, failure.value,
        )
        return TranscriptResult(None, failure)

def _build_api() -> YouTubeTranscriptApi:
    global _PROXY_WARNED
    if settings.webshare_proxy_username and settings.webshare_proxy_password:
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=settings.webshare_proxy_username,
                proxy_password=settings.webshare_proxy_password,
            )
        )

    # Direct connection is fine locally and fatal on Render — YouTube blocks
    # the transcript endpoint for datacenter IPs. Failing loudly in prod is
    # the point: a silent fallback here is indistinguishable from YouTube
    # rate-limiting us, which is exactly the ambiguity this audit exposed.
    if getattr(settings, "environment", "development") == "production":
        raise RuntimeError(
            "Webshare proxy credentials are not configured. YouTube blocks the "
            "transcript endpoint for datacenter IPs, so transcript fetching "
            "cannot work on Render without them. Set WEBSHARE_PROXY_USERNAME "
            "and WEBSHARE_PROXY_PASSWORD."
        )
    # Once per process, not once per call. _build_api() runs on every single
    # transcript fetch, so an unconditional warning buries a run's real output
    # under one identical line per video — and a retry-transcripts batch of 20
    # printed it 20 times. The guard replaces the unconditional warning rather
    # than sitting next to it.
    if not _PROXY_WARNED:
        logger.warning(
            "Webshare proxy not configured — using a direct connection. "
            "This works locally but will be IP-blocked on Render."
        )
        _PROXY_WARNED = True

    return YouTubeTranscriptApi()
