"""The feed crawl and bulk scrape limited to the sites switched on (plan step 11, item 8),
and the pages a crawl no longer finds. The crawl itself is stubbed: no site is reached."""
import asyncio

import pytest
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.scraper import bulk_scraper
from app.scraper.models import ScrapeTarget

pytestmark = pytest.mark.integration

ROG = "https://rog.asus.com/my/laptops/rog-strix/g16/"
ASUS = "https://www.asus.com/my/laptops/vivobook-14/"
GONE = "https://rog.asus.com/my/laptops/rog-zephyrus/old/"
OTHER_SITE_GONE = "https://www.asus.com/my/laptops/old-vivobook/"


@pytest.fixture
def asus(session, monkeypatch):
    brand = LaptopBrand(name="ASUS", base_scrape_url="https://example.invalid/asus")
    session.add(brand)
    session.commit()
    session.add_all([ScrapeTarget(url=GONE, brand_id=brand.id), ScrapeTarget(url=OTHER_SITE_GONE, brand_id=brand.id)])
    session.commit()

    async def crawl(start_url, session, brand_id):
        return [ROG, ASUS]

    monkeypatch.setitem(bulk_scraper.SCRAPERS, "asus", (crawl, bulk_scraper.SCRAPERS["asus"][1]))
    return brand


def test_only_the_sites_switched_on_are_queued_and_a_missing_page_is_reported(session, asus):
    result = asyncio.run(bulk_scraper.run_feed_crawl(asus.id, session, hosts=["rog.asus.com"]))

    urls = set(session.exec(select(ScrapeTarget.url).where(ScrapeTarget.brand_id == asus.id)).all())
    assert urls == {ROG, GONE, OTHER_SITE_GONE}, "a www.asus.com page was queued with only ROG switched on"
    assert (result["total_found"], result["added_to_queue"]) == (1, 1)
    assert result["missing_pages"] == [GONE], "only the switched-on site's pages can be missing"


def test_without_hosts_every_site_is_queued_as_before(session, asus):
    result = asyncio.run(bulk_scraper.run_feed_crawl(asus.id, session))
    assert (result["total_found"], result["added_to_queue"]) == (2, 2)
    assert result["missing_pages"] == [GONE, OTHER_SITE_GONE]


def test_a_crawl_that_finds_nothing_reports_no_missing_pages(session, asus, monkeypatch):
    async def nothing(start_url, session, brand_id):
        return []

    monkeypatch.setitem(bulk_scraper.SCRAPERS, "asus", (nothing, bulk_scraper.SCRAPERS["asus"][1]))
    assert asyncio.run(bulk_scraper.run_feed_crawl(asus.id, session))["missing_pages"] == []


def test_the_bulk_scrape_skips_the_sites_switched_off(session, asus, monkeypatch):
    scraped = []

    async def process(session, target, brand, report, progress):
        scraped.append(target.url)

    monkeypatch.setattr(bulk_scraper, "_process_target", process)
    report = asyncio.run(bulk_scraper.run_bulk_scrape(asus.id, session, hosts=["rog.asus.com"]))
    assert scraped == [GONE] and report.total_pending == 1
