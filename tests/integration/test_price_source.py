"""Where a price came from, and the SKU the product guides key on.

The guide import may only overwrite a price it wrote itself, so every path that
writes a price has to say so.
"""
import uuid

import pytest
from langchain_core.runnables import RunnableLambda
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.laptops.laptop_models import Laptop, LaptopPriceHistory, PriceSource
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def _history(session, laptop_id):
    return session.exec(select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == laptop_id)).all()


def _process(session, monkeypatch, brand, laptops, sku: str | None = "G835L-XGTQ284WP"):
    """Runs the upsert with one extracted variant per laptop in *laptops*."""
    raw = RawScrapLaptop(
        source_url=f"https://example.invalid/{uuid.uuid4().hex}", brand_id=brand.id, raw_product_name="Test Laptop",
        raw_specs_dump={"scraped_features": {"SKU": sku}},
    )
    session.add(raw)
    session.commit()

    def variant_of(laptop):
        fields = {name: getattr(laptop, name, None) for name in ExtractedLaptopVariant.model_fields}
        fields.update(categories=[], unmapped_specs={"test": True})
        return ExtractedLaptopVariant.model_construct(**fields)

    extracted = ExtractedLaptopFamily.model_construct(variants=[variant_of(l) for l in laptops])

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result


def _existing(session, brand, code, **fields):
    laptop = make_laptop(brand.id, model_code=code, **fields)
    session.add(laptop)
    session.commit()
    return laptop


def test_one_variant_gets_the_rows_sku(session, brand, monkeypatch):
    laptop = _existing(session, brand, "sku-one")

    _process(session, monkeypatch, brand, [laptop])

    session.refresh(laptop)
    assert laptop.manufacturer_sku == "G835L-XGTQ284WP"


def test_two_variants_get_no_sku_because_which_one_would_be_a_guess(session, brand, monkeypatch):
    first, second = _existing(session, brand, "sku-a"), _existing(session, brand, "sku-b")

    _process(session, monkeypatch, brand, [first, second])

    session.refresh(first)
    session.refresh(second)
    assert (first.manufacturer_sku, second.manufacturer_sku) == (None, None)


def test_a_new_laptop_gets_the_sku_on_insert(session, brand, monkeypatch):
    laptop = make_laptop(brand.id, model_code="sku-new")  # never added: the processor inserts it

    _process(session, monkeypatch, brand, [laptop])

    stored = session.exec(select(Laptop).where(Laptop.model_code == "sku-new")).one()
    assert stored.manufacturer_sku == "G835L-XGTQ284WP"


def test_reprocessing_never_overwrites_a_stored_sku(session, brand, monkeypatch):
    laptop = _existing(session, brand, "sku-kept", manufacturer_sku="G835L-ORIGINAL")

    _process(session, monkeypatch, brand, [laptop], sku="G835L-OTHER")

    session.refresh(laptop)
    assert laptop.manufacturer_sku == "G835L-ORIGINAL"


def _row(brand, code, price):
    """What the LLM extracted: never added, so only the processor can store it."""
    return make_laptop(brand.id, model_code=code, price_rm=price)


def _codes(session, brand):
    return set(session.exec(select(Laptop.model_code).where(Laptop.brand_id == brand.id)).all())


def test_the_same_sku_under_a_new_slug_updates_the_laptop_holding_it(session, brand, monkeypatch):
    laptop = _existing(session, brand, "slug-first", manufacturer_sku="G835L-XGTQ284WP", price_rm=5000.0)

    _process(session, monkeypatch, brand, [_row(brand, "slug-second", 5500.0)])

    session.refresh(laptop)
    assert _codes(session, brand) == {"slug-first"}  # the key stays, whatever the LLM wrote
    assert laptop.price_rm == 5500.0


def test_a_sku_match_beats_a_slug_match_on_another_laptop(session, brand, monkeypatch):
    holder = _existing(session, brand, "sku-holder", manufacturer_sku="G835L-XGTQ284WP", price_rm=5000.0)
    slug_row = _existing(session, brand, "sku-second", price_rm=4000.0)

    _process(session, monkeypatch, brand, [_row(brand, "sku-second", 5500.0)])

    session.refresh(holder)
    session.refresh(slug_row)
    assert (holder.price_rm, slug_row.price_rm, slug_row.manufacturer_sku) == (5500.0, 4000.0, None)


@pytest.mark.parametrize("stored, scraped", [("G835L-XGTQ284W", "G835L-XGTQ284WP"), ("G835L-XGTQ284WP", "G835L-XGTQ284W")])
def test_w_and_wp_are_one_sku_and_the_stored_form_stays(session, brand, monkeypatch, stored, scraped):
    laptop = _existing(session, brand, "wp-held", manufacturer_sku=stored, price_rm=5000.0)

    _process(session, monkeypatch, brand, [_row(brand, "wp-new-slug", 5500.0)], sku=scraped)

    session.refresh(laptop)
    assert _codes(session, brand) == {"wp-held"}
    assert (laptop.price_rm, laptop.manufacturer_sku) == (5500.0, stored)


def test_with_both_forms_stored_the_exact_one_is_updated(session, brand, monkeypatch):
    plain = _existing(session, brand, "wp-plain", manufacturer_sku="G835L-XGTQ284W", price_rm=5000.0)
    office = _existing(session, brand, "wp-office", manufacturer_sku="G835L-XGTQ284WP", price_rm=5000.0)

    _process(session, monkeypatch, brand, [_row(brand, "wp-third", 5500.0)], sku="G835L-XGTQ284WP")

    session.refresh(plain)
    session.refresh(office)
    assert (plain.price_rm, office.price_rm) == (5000.0, 5500.0)


def test_different_skus_under_different_slugs_stay_separate(session, brand, monkeypatch):
    laptop = _existing(session, brand, "sku-a", manufacturer_sku="G835L-AAA111W", price_rm=5000.0)

    _process(session, monkeypatch, brand, [_row(brand, "sku-b", 5500.0)], sku="G835L-BBB222W")

    session.refresh(laptop)
    new = session.exec(select(Laptop).where(Laptop.model_code == "sku-b")).one()
    assert (laptop.price_rm, new.manufacturer_sku) == (5000.0, "G835L-BBB222W")


def test_two_skus_of_one_spec_share_a_slug_but_stay_separate(session, brand, monkeypatch):
    """A slug spells out specs, so it cannot tell two SKUs of one spec apart."""
    laptop = _existing(session, brand, "same-spec", manufacturer_sku="G835L-AAA111W", price_rm=5000.0)

    for _ in range(2):  # the second run finds the new row by its SKU: no third row
        _process(session, monkeypatch, brand, [_row(brand, "same-spec", 5500.0)], sku="G835L-BBB222W")

    session.refresh(laptop)
    new = session.exec(select(Laptop).where(Laptop.manufacturer_sku == "G835L-BBB222W")).one()
    assert (laptop.price_rm, new.price_rm) == (5000.0, 5500.0)
    assert _codes(session, brand) == {"same-spec", new.model_code}


def test_with_no_sku_the_slug_decides_as_before(session, brand, monkeypatch):
    laptop = _existing(session, brand, "no-sku-slug", price_rm=5000.0)

    _process(session, monkeypatch, brand, [_row(brand, "no-sku-slug", 5500.0)], sku=None)

    session.refresh(laptop)
    assert _codes(session, brand) == {"no-sku-slug"}
    assert (laptop.price_rm, laptop.manufacturer_sku) == (5500.0, None)


def test_a_new_slug_with_no_sku_is_a_new_laptop(session, brand, monkeypatch):
    _existing(session, brand, "no-sku-old", manufacturer_sku="G835L-AAA111W")

    _process(session, monkeypatch, brand, [_row(brand, "no-sku-new", 5500.0)], sku=None)

    assert _codes(session, brand) == {"no-sku-old", "no-sku-new"}


def test_a_suspended_laptop_is_updated_but_stays_suspended(session, brand, monkeypatch):
    laptop = _existing(session, brand, "retired", manufacturer_sku="G835L-XGTQ284WP", status="suspended")

    _process(session, monkeypatch, brand, [_row(brand, "retired-new-slug", 5500.0)])

    session.refresh(laptop)
    assert _codes(session, brand) == {"retired"}
    assert (laptop.status, laptop.price_rm) == ("suspended", 5500.0)


def test_the_database_refuses_one_sku_on_two_laptops_but_allows_many_nulls(session, brand):
    _existing(session, brand, "null-a")
    _existing(session, brand, "null-b")
    _existing(session, brand, "held", manufacturer_sku="SKU-1")

    session.add(make_laptop(brand.id, model_code="clash", manufacturer_sku="SKU-1"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_the_processor_records_its_prices_as_page(session, brand, monkeypatch):
    laptop = _existing(session, brand, "src-page", price_rm=5000.0)
    changed = make_laptop(brand.id, model_code="src-page", price_rm=5500.0)

    _process(session, monkeypatch, brand, [changed])

    assert [(h.price_rm, h.source) for h in _history(session, laptop.id)] == [(5500.0, PriceSource.PAGE.value)]


def test_a_new_laptop_from_the_processor_records_page(session, brand, monkeypatch):
    _process(session, monkeypatch, brand, [make_laptop(brand.id, model_code="src-new", price_rm=5000.0)])

    stored = session.exec(select(Laptop).where(Laptop.model_code == "src-new")).one()
    assert [h.source for h in _history(session, stored.id)] == [PriceSource.PAGE.value]


def _body(brand, code, price):
    laptop = make_laptop(brand.id, model_code=code, price_rm=price)
    return {
        k: (str(v) if k == "brand_id" else v)
        for k, v in laptop.model_dump().items()
        if k in ("brand_id", "model_code", "product_name", "price_rm", "processor_model",
                 "gpu_model", "ram_gb", "ssd_gb", "display_size_inch", "weight_kg", "battery_wh")
    }


def test_an_admin_create_and_edit_record_manual(admin_client, session, brand):
    created = admin_client.post("/api/v2/laptops/", json=_body(brand, "src-manual", 4000.0))
    assert created.status_code == 201, created.text
    laptop_id = created.json()["id"]

    edited = admin_client.put(f"/api/v2/laptops/{laptop_id}", json={"price_rm": 4500.0})
    assert edited.status_code == 200, edited.text

    assert sorted((h.price_rm, h.source) for h in _history(session, laptop_id)) == [
        (4000.0, PriceSource.MANUAL.value), (4500.0, PriceSource.MANUAL.value),
    ]
