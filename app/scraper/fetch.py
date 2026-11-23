"""One plain HTTP GET for the live scrapers."""
from functools import partial
from urllib.parse import urlsplit

from scrapling.fetchers import Fetcher


def _path(url: str) -> str:
    # Case and a trailing slash are not a different page: rog.asus.com redirects
    # /ROG-Flow-Z13-2025/ to /rog-flow-z13-2025/ on a live page.
    return urlsplit(url).path.rstrip("/").lower()


def fetch_html(url: str, same_page: bool = False) -> str:
    """Sync: callers run it through asyncio.to_thread. *same_page*: raise when a redirect
    lands on another path. HP and ASUS answer a removed product page with a 200 on a
    listing page, so without it a removed page reads as a page whose layout changed."""
    page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True, timeout=30)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status} from {url}")
    if same_page and _path(page.url) != _path(url):
        raise RuntimeError(f"Redirected to {page.url}")
    return page.body.decode("utf-8", "replace")


# HP and ASUS/ROG product pages (and their listings, whose paths never redirect).
# Not Apple or PassMark: their redirects were never checked.
fetch_same_page = partial(fetch_html, same_page=True)
