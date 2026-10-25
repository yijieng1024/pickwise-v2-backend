"""
laptops.price_checked_at ("Last Checked") at its four write sites, plus the
PUT rule that a price of 0 never enters laptop_price_history.

Last Checked means "a scrape or an admin confirmed this price", changed or
not. It must move when a price is confirmed and must NOT move when nothing
was confirmed -- the admin form sends every field on every save, so a stray
bump would make the date mean "someone saved this page".
"""
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _history(session, laptop_id):
    return [h.price_rm for h in session.exec(
        select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == laptop_id)
    ).all()]


def _bumped(laptop) -> bool:
    return laptop.price_checked_at is not None and laptop.price_checked_at > OLD


def _run_processor(session, monkeypatch, brand, variants):
    """The LLM is stubbed: this is about what the upsert writes, not extraction."""
    from langchain_core.runnables import RunnableLambda

    from app.processor import engine as processor
    from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
    from app.scraper.models import RawScrapLaptop

    def variant_of(laptop, price):
        fields = {name: getattr(laptop, name, None) for name in ExtractedLaptopVariant.model_fields}
        fields.update(categories=[], unmapped_specs={"test": True}, price_rm=price)
        return ExtractedLaptopVariant.model_construct(**fields)

    extracted = ExtractedLaptopFamily.model_construct(
        variants=[variant_of(laptop, price) for laptop, price in variants]
    )

    class _StubLLM:
        def __init__(self, **_):
            pass

        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    raw = RawScrapLaptop(source_url="https://example.invalid/checked", brand_id=brand.id,
                         raw_product_name="Test Laptop")
    session.add(raw)
    session.commit()
    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result


def test_processor_update_bumps_on_a_confirmed_price_even_if_unchanged(session, brand, monkeypatch):
    """Update branch. Same price re-scraped = confirmed (bump, no history row);
    a no-price extraction confirmed nothing (no bump)."""
    same = make_laptop(brand.id, model_code="checked-same", price_rm=5999.0, price_checked_at=OLD)
    unpriced = make_laptop(brand.id, model_code="checked-unpriced", price_rm=5999.0, price_checked_at=OLD)
    session.add_all([same, unpriced])
    session.commit()

    _run_processor(session, monkeypatch, brand, [(same, 5999.0), (unpriced, 0.0)])

    session.refresh(same)
    session.refresh(unpriced)
    assert _bumped(same), "an unchanged but re-confirmed price must move Last Checked"
    assert _history(session, same.id) == [], "an unchanged price is not a history point"
    assert unpriced.price_checked_at == OLD, "an extraction with no price confirmed nothing"


def test_processor_insert_sets_the_date_only_when_priced(session, brand, monkeypatch):
    """Insert branch."""
    priced = make_laptop(brand.id, model_code="checked-new-priced", price_rm=4999.0)
    unpriced = make_laptop(brand.id, model_code="checked-new-unpriced", price_rm=0.0)

    _run_processor(session, monkeypatch, brand, [(priced, 4999.0), (unpriced, 0.0)])

    rows = {l.model_code: l for l in session.exec(
        select(Laptop).where(Laptop.model_code.in_(["checked-new-priced", "checked-new-unpriced"]))
    ).all()}
    assert rows["checked-new-priced"].price_checked_at is not None
    assert rows["checked-new-unpriced"].price_checked_at is None


def _create_body(brand, model_code, price):
    laptop = make_laptop(brand.id, model_code=model_code, price_rm=price)
    return {
        k: (str(v) if k == "brand_id" else v)
        for k, v in laptop.model_dump().items()
        if k in ("brand_id", "model_code", "product_name", "price_rm", "processor_model",
                 "gpu_model", "ram_gb", "ssd_gb", "display_size_inch", "weight_kg", "battery_wh")
    }


def test_create_sets_the_date_only_when_priced(admin_client, brand):
    """POST /laptops/. The column cannot be set from the body either."""
    body = _create_body(brand, "checked-post-priced", 4999.0)
    body["price_checked_at"] = "2020-01-01T00:00:00Z"
    priced = admin_client.post("/api/v2/laptops/", json=body)
    unpriced = admin_client.post("/api/v2/laptops/", json=_create_body(brand, "checked-post-unpriced", 0.0))
    assert priced.status_code == 201, priced.text
    assert unpriced.status_code == 201, unpriced.text

    assert priced.json()["price_checked_at"] is not None
    assert not priced.json()["price_checked_at"].startswith("2020"), "the body must not set it"
    assert unpriced.json()["price_checked_at"] is None


@pytest.fixture
def stored(session, brand):
    laptop = make_laptop(brand.id, model_code="checked-put", price_rm=5999.0, price_checked_at=OLD)
    session.add(laptop)
    session.commit()
    return laptop


def _put(admin_client, session, laptop, body):
    response = admin_client.put(f"/api/v2/laptops/{laptop.id}", json=body)
    assert response.status_code == 200, response.text
    session.refresh(laptop)
    return laptop


def test_put_unchanged_price_does_not_bump(admin_client, session, stored):
    """The admin form sends price_rm on every save; saving an unrelated edit
    is not a price check."""
    laptop = _put(admin_client, session, stored, {"price_rm": 5999.0, "ram_gb": 32})
    assert laptop.price_checked_at == OLD
    assert _history(session, laptop.id) == []


def test_put_changed_price_bumps_and_records_history(admin_client, session, stored):
    laptop = _put(admin_client, session, stored, {"price_rm": 6499.0})
    assert _bumped(laptop)
    assert _history(session, laptop.id) == [6499.0]


def test_put_price_confirmed_bumps_without_a_price_change(admin_client, session, stored):
    laptop = _put(admin_client, session, stored, {"price_rm": 5999.0, "price_confirmed": True})
    assert _bumped(laptop)
    assert _history(session, laptop.id) == [], "a confirmation is not a price change"


def test_put_price_to_zero_writes_no_history_and_no_date(admin_client, session, stored):
    """0 is "price unknown": not a point in the series, and nothing confirmed --
    even with the checkbox ticked."""
    laptop = _put(admin_client, session, stored, {"price_rm": 0.0, "price_confirmed": True})
    assert _history(session, laptop.id) == []
    assert laptop.price_checked_at == OLD
    assert laptop.status == "inactive"  # the unpriced mapper event still applies


def test_detail_carries_the_date_and_marketplace_links(api_client, session, stored):
    response = api_client.get(f"/api/v2/laptops/{stored.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["price_checked_at"].startswith("2026-01-01")
    assert body["shopee_search_url"].startswith("https://shopee.com.my/search?keyword=")
    assert body["lazada_search_url"].startswith("https://www.lazada.com.my/catalog/?q=")
