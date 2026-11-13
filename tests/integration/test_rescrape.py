"""
ROG and HP pages are scraped again every run (plan step 11, decisions 6 and 7).

Their pages carry the maker's SKU, so the processor finds the same laptop again. A
re-scrape replaces the page's raw row; a row whose name, prices and specs did not
change is left as it is and never reaches the processor. www.asus.com pages have no
SKU and keep the old rule: scraped once.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from langchain_core.runnables import RunnableLambda
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper import bulk_scraper
from app.scraper.models import RawScrapLaptop, ScrapeTarget
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

HP_URL = "https://www.hp.com/my-en/shop/hp-laptop-15-fd1574tu-d93cdpa.html"
ROG_URL = "https://rog.asus.com/my/laptops/rog-strix/rog-strix-g16-2025-g614/spec/"
WWW_URL = "https://www.asus.com/my/laptops/for-home/vivobook/asus-vivobook-s14-s3407/techspec/"


def _variant(sku_key: str, sku: str, price: str, suffix: str = "") -> dict:
    """One scraped variant, shaped as the brand scrapers return it."""
    return {"status": "success", "product_name": f"Laptop {sku}", "raw_prices_list": [{"price": price}],
            "image_urls": [], "raw_specs": {sku_key: sku, "Price": price, "Memory": "16GB"},
            "source_url_suffix": suffix}


def _stored(session, brand, url, variant: dict) -> RawScrapLaptop:
    """The raw row an earlier scrape of *variant* left, already processed."""
    row = RawScrapLaptop(source_url=url + variant["source_url_suffix"], brand_id=brand.id,
                         raw_product_name=variant["product_name"], raw_prices=variant["raw_prices_list"],
                         raw_specs_dump={"scraped_features": variant["raw_specs"]}, processing_status="completed")
    session.add(row)
    session.commit()
    return row


def _target(session, brand, url) -> ScrapeTarget:
    target = ScrapeTarget(url=url, brand_id=brand.id, scrape_status="completed",
                          last_scraped_at=datetime.now(timezone.utc))
    session.add(target)
    session.commit()
    return target


def _scrape(session, monkeypatch, brand, pages: dict) -> tuple:
    """A bulk run of *brand* against *pages* (url -> variants). Returns the report and
    the URLs it fetched."""
    fetched = []

    async def fake(_brand_name, url, *_a):
        fetched.append(url)
        return pages[url]

    monkeypatch.setattr(bulk_scraper, "_dispatch_scraper", fake)
    monkeypatch.setattr(bulk_scraper, "LIVE_FETCH_PAUSE_S", 0)
    return asyncio.run(bulk_scraper.run_bulk_scrape(brand.id, session)), fetched


def _stub_llm(monkeypatch, template: Laptop, by_sku: dict):
    """The LLM extracts one variant per raw row: the price in *by_sku* for the SKU its
    prompt carries, under a slug no stored laptop has, so only the SKU can match."""
    def extract(prompt):
        text = prompt.to_string()
        sku = next(s for s in by_sku if s in text)
        fields = {name: getattr(template, name, None) for name in ExtractedLaptopVariant.model_fields}
        fields.update(categories=[], unmapped_specs={"test": True}, model_code=f"new-wording-{sku.lower()}",
                      price_rm=by_sku[sku])
        return ExtractedLaptopFamily.model_construct(variants=[ExtractedLaptopVariant.model_construct(**fields)])

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(extract)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())


def _hp(session) -> LaptopBrand:
    brand = LaptopBrand(name="HP", base_scrape_url="https://example.invalid/hp")
    session.add(brand)
    session.commit()
    return brand


def test_unchanged_content_is_not_processed(session, monkeypatch):
    brand = _hp(session)
    page = _variant("Product Info / Sku", "D93CDPA", "RM2,999.00")
    stored = _stored(session, brand, HP_URL, page)
    _target(session, brand, HP_URL)
    stored_id = stored.id

    report, fetched = _scrape(session, monkeypatch, brand, {HP_URL: [page]})

    assert fetched == [HP_URL], "the HP page was not fetched again"
    [row] = session.exec(select(RawScrapLaptop).where(RawScrapLaptop.source_url == HP_URL)).all()
    assert (row.id, row.processing_status) == (stored_id, "completed"), "an unchanged page was queued for the processor"
    assert (report.skipped, report.succeeded, report.failed) == (1, 0, 0)
    assert processor.process_pending_laptops(session)["requests_made"] == 0


def test_a_changed_price_updates_the_laptop_and_adds_one_history_row(session, monkeypatch):
    brand = _hp(session)
    laptop = make_laptop(brand.id, model_code="hp-15-fd1574tu", price_rm=2999.0)
    laptop.manufacturer_sku = "D93CDPA"
    session.add(laptop)
    session.commit()
    session.add(LaptopPriceHistory(laptop_id=laptop.id, price_rm=2999.0, source="page"))
    stored_id = _stored(session, brand, HP_URL, _variant("Product Info / Sku", "D93CDPA", "RM2,999.00")).id
    _target(session, brand, HP_URL)

    report, _ = _scrape(session, monkeypatch, brand,
                        {HP_URL: [_variant("Product Info / Sku", "D93CDPA", "RM2,799.00")]})

    [row] = session.exec(select(RawScrapLaptop).where(RawScrapLaptop.source_url == HP_URL)).all()
    assert row.id != stored_id and row.processing_status == "pending", "the changed page did not replace its raw row"
    assert row.raw_prices == [{"price": "RM2,799.00"}]
    assert report.succeeded == 1

    _stub_llm(monkeypatch, laptop, {"D93CDPA": 2799.0})
    processor.process_pending_laptops(session)

    assert [l.id for l in session.exec(select(Laptop).where(Laptop.brand_id == brand.id)).all()] == [laptop.id]
    session.refresh(laptop)
    assert laptop.price_rm == 2799.0
    history = session.exec(select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == laptop.id)
                           .order_by(LaptopPriceHistory.recorded_at)).all()  # type: ignore[arg-type]
    assert [(h.price_rm, h.source) for h in history] == [(2999.0, "page"), (2799.0, "page")]


def test_a_www_asus_target_is_not_scraped_again(session, brand, monkeypatch):
    # `brand` is Asus: one brand, two sites, and only rog.asus.com has a SKU.
    rog = _variant("SKU", "G614PR-S5099W", "RM9,099.00")
    www = _variant("Model", "S3407", "N/A")
    _stored(session, brand, ROG_URL, rog)
    www_id = _stored(session, brand, WWW_URL, www).id
    _target(session, brand, ROG_URL)
    _target(session, brand, WWW_URL)

    _, fetched = _scrape(session, monkeypatch, brand, {ROG_URL: [rog], WWW_URL: [{**www, "product_name": "Changed"}]})

    assert fetched == [ROG_URL]
    [row] = session.exec(select(RawScrapLaptop).where(RawScrapLaptop.source_url == WWW_URL)).all()
    assert (row.id, row.raw_product_name) == (www_id, "Laptop S3407")


def test_a_multi_variant_rog_page_updates_each_variant_by_its_own_sku(session, brand, monkeypatch):
    skus = {"G614PR-S5099W": (9099.0, 8599.0), "G614PM-S5033W": (7499.0, 7199.0)}  # stored, then on the page
    laptops = {}
    for n, (sku, (was, _now)) in enumerate(skus.items(), start=1):
        laptop = make_laptop(brand.id, model_code=f"rog-strix-g16-v{n}", price_rm=was)
        laptop.manufacturer_sku = sku
        session.add(laptop)
        laptops[sku] = laptop
        _stored(session, brand, ROG_URL, _variant("SKU", sku, f"RM{was:,.2f}", f"?v={n}"))
    session.commit()
    _target(session, brand, ROG_URL)

    page = [_variant("SKU", sku, f"RM{now:,.2f}", f"?v={n}") for n, (sku, (_was, now)) in enumerate(skus.items(), 1)]
    report, _ = _scrape(session, monkeypatch, brand, {ROG_URL: page})
    assert report.succeeded == 1 and report.results[-1].error == "2 variant(s) saved"

    _stub_llm(monkeypatch, laptops["G614PR-S5099W"], {sku: now for sku, (_was, now) in skus.items()})
    processor.process_pending_laptops(session)

    rows = session.exec(select(Laptop).where(Laptop.brand_id == brand.id)).all()
    assert {r.id for r in rows} == {l.id for l in laptops.values()}, "a re-scraped variant made a new laptop"
    for sku, (_was, now) in skus.items():
        session.refresh(laptops[sku])
        assert (laptops[sku].manufacturer_sku, laptops[sku].price_rm) == (sku, now)
