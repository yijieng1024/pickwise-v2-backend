"""One plain HTTP GET for the live scrapers."""
from scrapling.fetchers import Fetcher


def fetch_html(url: str) -> str:
    """Sync: callers run it through asyncio.to_thread."""
    page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True, timeout=30)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status} from {url}")
    return page.body.decode("utf-8", "replace")
