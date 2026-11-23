"""A removed HP or ASUS page answers 200 on a listing page: the fetch calls that a
redirect, and the crawl checks the missing pages a bulk run never fetches again.
Fetcher stood in: no site is reached."""
import asyncio
from types import SimpleNamespace

import pytest

from ._adapters import bulk_scraper, scraper_fetch

PAGE = "https://www.hp.com/my-en/shop/hp-laptop-14-em0289au-dj6m3pa.html"


def _answer(monkeypatch, final_url: str, status: int = 200):
    monkeypatch.setattr(scraper_fetch.Fetcher, "get", lambda url, **_: SimpleNamespace(
        status=status, url=final_url, body=b"<html></html>"))


def test_a_redirect_to_another_page_is_reported_as_one(monkeypatch):
    _answer(monkeypatch, "https://www.hp.com/my-en/shop/laptops-tablets.html")
    with pytest.raises(RuntimeError, match="^Redirected to https://www.hp.com/my-en/shop/laptops-tablets.html$"):
        scraper_fetch.fetch_same_page(PAGE)


@pytest.mark.parametrize("asked, landed", [
    (PAGE, PAGE),
    # rog.asus.com lowercases a live page's path; a slash is not a page either.
    ("https://rog.asus.com/my/laptops/rog-flow/ROG-Flow-Z13-2025/spec/",
     "https://rog.asus.com/my/laptops/rog-flow/rog-flow-z13-2025/spec"),
    # HP's listing pages carry ?p=N: a query is not a path.
    ("https://www.hp.com/my-en/shop/laptops-tablets.html?p=2", "https://www.hp.com/my-en/shop/laptops-tablets.html?p=2"),
])
def test_the_same_page_is_not_a_redirect(monkeypatch, asked, landed):
    _answer(monkeypatch, landed)
    assert scraper_fetch.fetch_same_page(asked) == "<html></html>"


def test_without_same_page_a_redirect_is_followed(monkeypatch):
    _answer(monkeypatch, "https://www.apple.com/my/elsewhere")
    assert scraper_fetch.fetch_html("https://www.apple.com/my/mac") == "<html></html>"


def test_a_404_still_says_404(monkeypatch):
    _answer(monkeypatch, PAGE, status=404)
    with pytest.raises(RuntimeError, match="^HTTP 404 from"):
        scraper_fetch.fetch_same_page(PAGE)


def test_the_crawl_checks_only_the_sites_a_run_never_fetches_again(monkeypatch):
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url.endswith("/gone/"):
            raise RuntimeError("Redirected to https://www.asus.com/my/laptops/for-creators/proart/")
        return ""

    monkeypatch.setattr(bulk_scraper, "fetch_same_page", fetch)
    monkeypatch.setattr(bulk_scraper, "LIVE_FETCH_PAUSE_S", 0)
    urls = ["https://www.asus.com/my/gone/", "https://www.asus.com/my/still/",
            # Re-scraped every run, so the scrape step's own fetch says it.
            "https://rog.asus.com/my/x/", PAGE]
    errors = asyncio.run(bulk_scraper.check_missing(urls))
    assert errors == {"https://www.asus.com/my/gone/":
                      "Redirected to https://www.asus.com/my/laptops/for-creators/proart/"}
    assert fetched == urls[:2]
