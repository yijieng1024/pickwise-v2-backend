"""
Writes to price_rm and laptop_price_history.

price_rm = 0 means "price unknown" (the engine scores it a flagged neutral 50).
A write path that PRODUCES a 0 over a real price, or records 0 as a point in
the price series, fabricates exactly that ambiguity: the laptop reads as "never
had a price" and the trend line gains a drop to zero that never happened.
"""

from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def _history(session, laptop_id):
    return session.exec(
        select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == laptop_id)
    ).all()


def test_reprocessing_without_a_price_keeps_the_live_price_and_says_so(
    session, brand, monkeypatch
):
    """
    Catches re-processing erasing a real price. The update branch setattr'd
    every extracted field, so an extraction that could not find the price
    (the schema tells the model to output 0.0) overwrote RM5,999 with 0 and
    left the laptop active, scored as "no price".

    Two variants in one run: one with no price (kept, and REPORTED -- silently
    keeping the old price hides the failed extraction), one with a new real
    price (still overwrites, so the fix is not "never update prices").

    The LLM and its API key are stubbed; this is about what the upsert does
    with the extraction, not about extraction.
    """
    from langchain_core.runnables import RunnableLambda

    from app.processor import engine as processor
    from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
    from app.scraper.models import RawScrapLaptop

    unpriced = make_laptop(brand.id, model_code="price-kept", price_rm=5999.0)
    repriced = make_laptop(brand.id, model_code="price-changed", price_rm=5999.0)
    raw = RawScrapLaptop(source_url="https://example.invalid/p", brand_id=brand.id,
                         raw_product_name="Test Laptop")
    session.add_all([unpriced, repriced, raw])
    session.commit()

    def variant_of(laptop, price):
        fields = {name: getattr(laptop, name, None) for name in ExtractedLaptopVariant.model_fields}
        fields.update(categories=[], unmapped_specs={"test": True}, price_rm=price)
        return ExtractedLaptopVariant.model_construct(**fields)

    extracted = ExtractedLaptopFamily.model_construct(
        variants=[variant_of(unpriced, 0.0), variant_of(repriced, 6499.0)]
    )

    class _StubLLM:
        def __init__(self, **_):
            pass

        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "ChatGoogleGenerativeAI", _StubLLM)
    monkeypatch.setattr(processor, "settings", SimpleNamespace(gemini_api_key="stub-never-sent"))

    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result

    session.refresh(unpriced)
    session.refresh(repriced)
    assert unpriced.price_rm == 5999.0, "an extraction with no price erased the live price"
    assert repriced.price_rm == 6499.0, "a real new price must still be written"
    assert result["prices_not_extracted"] == ["price-kept"], (
        "the kept price must be reported, or the failed extraction is invisible"
    )
    assert all(h.price_rm != 0 for h in _history(session, unpriced.id))


def test_creating_a_laptop_at_price_zero_writes_no_history_row(admin_client, brand, session):
    """
    Catches POST /laptops/ recording price 0 as a point in the price series. 0 is
    "unknown", not a price, and the series feeds trend charts: a zero there is a
    fabricated drop. A priced create still records its snapshot (the control).
    """
    def body(model_code, price):
        laptop = make_laptop(brand.id, model_code=model_code, price_rm=price)
        return {
            k: (str(v) if k == "brand_id" else v)
            for k, v in laptop.model_dump().items()
            if k in ("brand_id", "model_code", "product_name", "price_rm", "processor_model",
                     "gpu_model", "ram_gb", "ssd_gb", "display_size_inch", "weight_kg", "battery_wh")
        }

    unpriced = admin_client.post("/api/v2/laptops/", json=body("created-unpriced", 0.0))
    priced = admin_client.post("/api/v2/laptops/", json=body("created-priced", 4999.0))
    assert unpriced.status_code == 201, unpriced.text
    assert priced.status_code == 201, priced.text

    assert _history(session, unpriced.json()["id"]) == []
    assert [h.price_rm for h in _history(session, priced.json()["id"])] == [4999.0]


def test_a_laptop_with_no_price_is_not_left_active(session, brand):
    """
    price_rm = 0 is "price unknown", so the row belongs in the awaiting-a-price
    queue (ADR-0009's `inactive`), not in the recommendable set -- the agent
    would otherwise offer a machine whose price it cannot state.

    Covers both flush paths, and both things the rule must NOT do: touch a
    suspended row (that is the retired archive, not a work queue), or
    re-activate on its own when a price comes back.
    """
    created = make_laptop(brand.id, model_code="unpriced-insert", price_rm=0.0)
    demoted = make_laptop(brand.id, model_code="unpriced-update", price_rm=4999.0)
    retired = make_laptop(brand.id, model_code="unpriced-suspended", price_rm=0.0,
                          status="suspended")
    session.add_all([created, demoted, retired])
    session.commit()

    assert created.status == "inactive"      # insert path
    assert retired.status == "suspended"     # left alone

    demoted.price_rm = 0.0
    session.commit()
    assert demoted.status == "inactive"      # update path

    demoted.price_rm = 4999.0
    session.commit()
    assert demoted.status == "inactive"      # re-activating is an admin decision
"""
Writes to price_rm and laptop_price_history.

price_rm = 0 means "price unknown" (the engine scores it a flagged neutral 50).
A write path that PRODUCES a 0 over a real price, or records 0 as a point in
the price series, fabricates exactly that ambiguity: the laptop reads as "never
had a price" and the trend line gains a drop to zero that never happened.
"""

from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def _history(session, laptop_id):
    return session.exec(
        select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == laptop_id)
    ).all()


def test_reprocessing_without_a_price_keeps_the_live_price_and_says_so(
    session, brand, monkeypatch
):
    """
    Catches re-processing erasing a real price. The update branch setattr'd
    every extracted field, so an extraction that could not find the price
    (the schema tells the model to output 0.0) overwrote RM5,999 with 0 and
    left the laptop active, scored as "no price".

    Two variants in one run: one with no price (kept, and REPORTED -- silently
    keeping the old price hides the failed extraction), one with a new real
    price (still overwrites, so the fix is not "never update prices").

    The LLM and its API key are stubbed; this is about what the upsert does
    with the extraction, not about extraction.
    """
    from langchain_core.runnables import RunnableLambda

    from app.processor import engine as processor
    from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
    from app.scraper.models import RawScrapLaptop

    unpriced = make_laptop(brand.id, model_code="price-kept", price_rm=5999.0)
    repriced = make_laptop(brand.id, model_code="price-changed", price_rm=5999.0)
    raw = RawScrapLaptop(source_url="https://example.invalid/p", brand_id=brand.id,
                         raw_product_name="Test Laptop")
    session.add_all([unpriced, repriced, raw])
    session.commit()

    def variant_of(laptop, price):
        fields = {name: getattr(laptop, name, None) for name in ExtractedLaptopVariant.model_fields}
        fields.update(categories=[], unmapped_specs={"test": True}, price_rm=price)
        return ExtractedLaptopVariant.model_construct(**fields)

    extracted = ExtractedLaptopFamily.model_construct(
        variants=[variant_of(unpriced, 0.0), variant_of(repriced, 6499.0)]
    )

    class _StubLLM:
        def __init__(self, **_):
            pass

        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "ChatGoogleGenerativeAI", _StubLLM)
    monkeypatch.setattr(processor, "settings", SimpleNamespace(gemini_api_key="stub-never-sent"))

    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result

    session.refresh(unpriced)
    session.refresh(repriced)
    assert unpriced.price_rm == 5999.0, "an extraction with no price erased the live price"
    assert repriced.price_rm == 6499.0, "a real new price must still be written"
    assert result["prices_not_extracted"] == ["price-kept"], (
        "the kept price must be reported, or the failed extraction is invisible"
    )
    assert all(h.price_rm != 0 for h in _history(session, unpriced.id))


def test_creating_a_laptop_at_price_zero_writes_no_history_row(admin_client, brand, session):
    """
    Catches POST /laptops/ recording price 0 as a point in the price series. 0 is
    "unknown", not a price, and the series feeds trend charts: a zero there is a
    fabricated drop. A priced create still records its snapshot (the control).
    """
    def body(model_code, price):
        laptop = make_laptop(brand.id, model_code=model_code, price_rm=price)
        return {
            k: (str(v) if k == "brand_id" else v)
            for k, v in laptop.model_dump().items()
            if k in ("brand_id", "model_code", "product_name", "price_rm", "processor_model",
                     "gpu_model", "ram_gb", "ssd_gb", "display_size_inch", "weight_kg", "battery_wh")
        }

    unpriced = admin_client.post("/api/v2/laptops/", json=body("created-unpriced", 0.0))
    priced = admin_client.post("/api/v2/laptops/", json=body("created-priced", 4999.0))
    assert unpriced.status_code == 201, unpriced.text
    assert priced.status_code == 201, priced.text

    assert _history(session, unpriced.json()["id"]) == []
    assert [h.price_rm for h in _history(session, priced.json()["id"])] == [4999.0]
