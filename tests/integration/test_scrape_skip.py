"""A page counts as already scraped only by its own URL or URL?v=N, never by a longer
URL it is a prefix of: .../buy-mac/macbook-pro was skipped because the PROLED buy page,
.../buy-mac/macbook-pro-proled-xdr, was stored."""
import pytest

from app.laptops.brand_model import LaptopBrand
from app.scraper.bulk_scraper import stored_rows_of
from app.scraper.models import RawScrapLaptop
from sqlmodel import select

pytestmark = pytest.mark.integration

PRO = "https://www.apple.com/my/shop/buy-mac/macbook-pro"


def test_prefix_url_is_not_already_scraped(session):
    brand = LaptopBrand(name="Apple", base_scrape_url="https://example.invalid")
    session.add(brand)
    session.commit()
    for url in (PRO + "-proled-xdr", "https://x.invalid/asus?v=2"):
        session.add(RawScrapLaptop(source_url=url, brand_id=brand.id, raw_product_name="x", raw_specs_dump={}))
    session.commit()

    assert session.exec(select(RawScrapLaptop).where(stored_rows_of(PRO))).first() is None
    assert session.exec(select(RawScrapLaptop).where(stored_rows_of("https://x.invalid/asus"))).first() is not None


def _target_run(session, monkeypatch, brand_name, url):
    import asyncio
    from app.scraper import bulk_scraper
    from app.scraper.models import ScrapeTarget

    brand = LaptopBrand(name=brand_name, base_scrape_url="https://example.invalid")
    session.add(brand)
    session.commit()
    session.add(RawScrapLaptop(source_url=url, brand_id=brand.id, raw_product_name="June", raw_specs_dump={}))
    target = ScrapeTarget(url=url, brand_id=brand.id)
    session.add(target)
    session.commit()

    async def fake(*_a, **_k):
        return [{"status": "success", "product_name": "Today", "raw_specs": ["today"]}]
    monkeypatch.setattr(bulk_scraper, "_dispatch_scraper", fake)
    monkeypatch.setattr(bulk_scraper, "LIVE_FETCH_PAUSE_S", 0)
    report = bulk_scraper.BulkScrapeReport(brand_name=brand_name, total_pending=1)
    asyncio.run(bulk_scraper._process_target(session, target, brand, report))
    return session.exec(select(RawScrapLaptop.raw_product_name).where(RawScrapLaptop.source_url == url)).all()


def test_apple_specs_page_is_scraped_again(session, monkeypatch):
    names = _target_run(session, monkeypatch, "Apple", "https://www.apple.com/my/macbook-neo/specs/")
    assert names == ["Today"], "a stored Apple specs page blocked today's scrape"


def test_other_brands_keep_the_skip_rule(session, monkeypatch):
    # www.asus.com: no SKU on its pages, so a stored page is never fetched again
    assert _target_run(session, monkeypatch, "Asus", "https://www.asus.com/my/x/techspec/") == ["June"]


def test_a_rescraped_page_with_no_sku_keeps_its_stored_row(session, monkeypatch):
    # HP pages are fetched again (step 11), but a fetch that carries no SKU does not
    # replace the stored row: the processor would match it by slug alone
    assert _target_run(session, monkeypatch, "HP", "https://www.hp.com/my-en/x/specs/") == ["June"]
