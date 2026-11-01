"""SCRAPERS in bulk_scraper.py is the one place that says which brands can be
crawled and scraped; both routes and the bulk path read it."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ._adapters import bulk_scraper, scraper_router


class _Session:
    """As much of a Session as the routes touch before they dispatch."""

    def __init__(self, brand):
        self.brand = brand

    def get(self, model, pk):
        return self.brand

    def exec(self, statement):
        return SimpleNamespace(first=lambda: None)

    def commit(self):
        pass


def _hp():
    return SimpleNamespace(id=uuid.uuid4(), name="HP", base_scrape_url="https://example.invalid/hp")


def test_scraper_brands_are_the_dict_and_upload_brands_are_among_them():
    assert bulk_scraper.SCRAPER_BRANDS == frozenset({"apple", "asus", "acer", "hp"})
    assert bulk_scraper.SCRAPER_BRANDS == frozenset(bulk_scraper.SCRAPERS)
    assert bulk_scraper.HTML_UPLOAD_BRANDS <= bulk_scraper.SCRAPER_BRANDS


def test_a_brand_missing_from_scrapers_gets_400_from_the_crawl_route(monkeypatch):
    monkeypatch.delitem(bulk_scraper.SCRAPERS, "hp")
    brand = _hp()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(scraper_router.feed_crawler_queue(
            scraper_router.CrawlerQueueRequest(brand_id=brand.id), _Session(brand),
        ))

    assert exc.value.status_code == 400
    assert exc.value.detail == "Currently, HP brand crawling is not supported."


def test_a_brand_missing_from_scrapers_gets_400_from_the_scrape_route(monkeypatch):
    monkeypatch.delitem(bulk_scraper.SCRAPERS, "hp")
    brand = _hp()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(scraper_router.scrape_url(
            scraper_router.ScraperRequest(url="https://example.invalid/p", brand_id=brand.id),
            _Session(brand),
        ))

    assert exc.value.status_code == 400
    assert exc.value.detail == "Currently, HP brand scraping is not supported."


def test_a_brand_missing_from_scrapers_is_a_value_error_in_a_bulk_run():
    with pytest.raises(ValueError, match="Bulk scraping is not supported for brand: Lenovo"):
        asyncio.run(bulk_scraper._dispatch_scraper("Lenovo", "https://example.invalid/p", uuid.uuid4(), None))


@pytest.mark.parametrize("name", ["apple", "asus", "acer", "hp"])
def test_every_entry_takes_the_same_arguments_and_returns_a_list(monkeypatch, name):
    # Each brand's own function has its own signature; the entry hides that.
    seen = {}

    async def fake(*args, **kwargs):
        seen["args"] = args
        return {"status": "success"} if name == "apple" else [{"status": "success"}]

    target = {
        "apple": "scrape_official_website",
        "asus": "scrape_asus_laptop_specs",
        "acer": "scrape_acer_laptop_specs",
        "hp": "scrape_hp_laptop_specs",
    }[name]
    monkeypatch.setattr(bulk_scraper, target, fake)
    brand_id, session = uuid.uuid4(), object()

    result = asyncio.run(bulk_scraper._dispatch_scraper(name.upper(), "https://example.invalid/p", brand_id, session))

    assert result == [{"status": "success"}]
    assert seen["args"][0] == "https://example.invalid/p"
    assert (session in seen["args"]) is (name == "acer")


def test_a_crawl_entry_takes_the_same_arguments_for_every_brand(monkeypatch):
    calls = {}

    async def fake(*args):
        calls["hp"] = args
        return ["u"]

    async def fake_acer(*args):
        calls["acer"] = args
        return ["u"]

    monkeypatch.setattr(bulk_scraper, "crawl_hp_specs_links", fake)
    monkeypatch.setattr(bulk_scraper, "crawl_acer_specs_links", fake_acer)
    session, brand_id = object(), uuid.uuid4()

    for name in ("hp", "acer"):
        assert asyncio.run(bulk_scraper.SCRAPERS[name][0]("https://example.invalid", session, brand_id)) == ["u"]

    assert calls["hp"] == ("https://example.invalid",)
    assert calls["acer"] == ("https://example.invalid", session, brand_id)
