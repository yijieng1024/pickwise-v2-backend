import re
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from app.config import settings
from app.logger import get_logger
from app.reviews.models import YoutubeChannel

logger = get_logger(__name__)

_MAX_RESULTS_PER_CHANNEL = 5

# How far back a discovery search reaches, in days. ~3 years.
#
# Without a bound, a search for a 2025 machine returns that channel's 2019
# videos of the same product line — and at maxResults=5 those stale hits do not
# merely add noise, they consume slots that a real review would have taken. The
# search costs 100 quota units per channel either way, so a wasted slot is a
# fifth of a paid request. Three years is wide enough to cover a laptop still
# on sale two generations after launch, and narrow enough to exclude the
# previous chassis era.
_DEFAULT_PUBLISHED_AFTER_DAYS = 365 * 3


def _get_youtube_client():
    if not settings.youtube_api_key:
        raise ValueError(
            "YouTube Data API key not configured. Add YOUTUBE_API_KEY=<your_key> to .env"
        )
    return build("youtube", "v3", developerKey=settings.youtube_api_key)


def _parse_url(channel_url: str) -> tuple[str, str]:
    """
    Parse a YouTube channel URL or bare handle/ID into (lookup_type, value).
    Supported formats:
      https://www.youtube.com/@MKBHD       → ("handle", "MKBHD")
      https://www.youtube.com/channel/UCxx → ("id", "UCxx")
      @MKBHD                               → ("handle", "MKBHD")
      UCBJycsmduvYEL83R_U4JriQ            → ("id", "UCxx...")
    """
    url = channel_url.strip()

    # Full URL with /channel/UCxx
    match = re.search(r"youtube\.com/channel/(UC[\w-]+)", url)
    if match:
        return "id", match.group(1)

    # Full URL with /@handle
    match = re.search(r"youtube\.com/@([\w.-]+)", url)
    if match:
        return "handle", match.group(1)

    # Bare @handle
    if url.startswith("@"):
        return "handle", url[1:]

    # Bare channel ID
    if url.startswith("UC") and len(url) > 10:
        return "id", url

    raise ValueError(
        f"Cannot parse YouTube channel URL: '{channel_url}'. "
        "Expected formats: https://youtube.com/@handle, "
        "https://youtube.com/channel/UCxx, @handle, or UCxx channel ID."
    )


def resolve_channel_from_url(channel_url: str) -> dict:
    """
    Resolve a YouTube channel URL to its channel_id, channel_name, and channel_img_url.
    Costs 1 quota unit. Requires YOUTUBE_API_KEY in .env.
    Returns dict with keys: channel_id, channel_name, channel_img_url.
    """
    youtube = _get_youtube_client()
    lookup_type, value = _parse_url(channel_url)

    kwargs = {"part": "snippet", "maxResults": 1}
    if lookup_type == "handle":
        kwargs["forHandle"] = value
    else:
        kwargs["id"] = value

    response = youtube.channels().list(**kwargs).execute()
    items = response.get("items", [])

    if not items:
        raise ValueError(
            f"No YouTube channel found for '{channel_url}'. "
            "Check the URL or handle and try again."
        )

    item = items[0]
    snippet = item["snippet"]
    thumbnails = snippet.get("thumbnails", {})
    img_url = (
        thumbnails.get("high", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
        or thumbnails.get("default", {}).get("url")
    )

    return {
        "channel_id": item["id"],
        "channel_name": snippet["title"],
        "channel_img_url": img_url,
    }


def discover_videos(
    brand_name: str,
    product_name: str,
    channels: list[YoutubeChannel],
    published_after_days: int = _DEFAULT_PUBLISHED_AFTER_DAYS,
) -> list[dict]:
    """
    Search YouTube for review videos of a specific laptop model across all active channels.
    Returns a list of raw video metadata dicts (video_id, channel_id, video_title, published_at).
    Costs 100 quota units per channel searched.

    `published_after_days` bounds how far back the search reaches. Pass 0 to
    disable the bound (a deliberate archive sweep), never as a default.
    """
    youtube = _get_youtube_client()
    query = f"{brand_name} {product_name} review"
    # RFC3339 with a literal Z — the Data API rejects a "+00:00" offset here.
    published_after = None
    if published_after_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=published_after_days)
        published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    results = []

    for channel in channels:
        if not channel.active:
            continue
        try:
            response = (
                youtube.search()
                .list(
                    part="snippet",
                    q=query,
                    channelId=channel.channel_id,
                    type="video",
                    maxResults=_MAX_RESULTS_PER_CHANNEL,
                    order="relevance",
                    **({"publishedAfter": published_after} if published_after else {}),
                )
                .execute()
            )
            for item in response.get("items", []):
                results.append(
                    {
                        "video_id": item["id"]["videoId"],
                        "channel_id": channel.channel_id,
                        "video_title": item["snippet"]["title"],
                        "published_at": item["snippet"]["publishedAt"],
                    }
                )
            logger.info(
                "Discovery: %d videos found for '%s' in channel %s (since %s)",
                len(response.get("items", [])),
                query,
                channel.channel_name,
                published_after or "beginning",
            )
        except Exception as e:
            logger.warning("Discovery failed for channel %s: %s", channel.channel_id, e)

    return results
