"""
The Apple buy-page processor (app/processor/engine.py + apple_buy.py, decided 2026-11-03).

Configurations and prices come from the buy page; the specs page gives the shared specs,
one LLM call per size section. Before this, the LLM chose the configurations from the
specs page, which lists options per chip, not what Apple sells, and a PROLED page once
overwrote a Liquid Retina row in place. The LLM is stubbed throughout.
"""
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import LaptopPriceHistory
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

SPECS = "https://www.apple.com/my/macbook-pro/liquid-retina-xdr-display/specs/"
BUY = "https://www.apple.com/my/shop/buy-mac/macbook-pro"
CODE = "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-1024gb"
NAME = "14-inch MacBook Pro Liquid Retina XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 1TB SSD)"


@pytest.fixture
def apple(session) -> LaptopBrand:
    row = LaptopBrand(name="Apple", base_scrape_url="https://example.invalid/apple")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _template(**fields) -> ExtractedLaptopVariant:
    """What the LLM gives for one chip of one size: specs, with memory and storage that
    the buy page replaces."""
    base = {name: None for name in ExtractedLaptopVariant.model_fields}
    base.update(
        model_code="llm-wording", product_name="MacBook Pro", price_rm=0.0,
        processor_model="Apple M5 Pro chip", gpu_model="16-core GPU", cpu_cores=15, gpu_cores=16, ram_gb=16,
        ssd_gb=512, display_size_inch=14.2, display_brightness_nits=1600, weight_kg=1.55, battery_wh=72.4,
        categories=[], unmapped_specs={"test": True}, colors=[], ports_summary=[], ai_features=[],
    )
    base.update(fields)
    return ExtractedLaptopVariant.model_construct(**base)


def _size(inch: int) -> str:
    return (f"\nFinish\nSilver\n\nChip\nApple M5 Pro chip\n\nSize and Weight\nWidth: {inch}cm\n"
            f"\nOperating System\nmacOS\n\nIn the Box\n{inch}-inch MacBook Pro\n\nEnvironment\n")


TWO_SIZES = {"scraped_features": ["Product image" + _size(14) + _size(16)]}


def _config(**over) -> dict:
    return {"size": 14, "chip": "M5 Pro", "cpu_cores": 15, "gpu_cores": 16, "ram_gb": 24, "ssd_gb": 1024,
            "slugs": ["s"], "part_numbers": ["MGDN4ZP/A", "MGDR4ZP/A"], "price_rm": 10299.0, **over}


def _stub(monkeypatch, *variants):
    """First size section answers *variants*, later ones none; records each call."""
    calls = []

    class _StubLLM:
        def with_structured_output(self, _schema):
            def extract(inputs):
                calls.append(inputs.to_string())
                return ExtractedLaptopFamily.model_construct(variants=list(variants) if len(calls) == 1 else [])
            return RunnableLambda(extract)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    return calls


def _pages(session, brand, configs, specs_dump=TWO_SIZES):
    specs = RawScrapLaptop(source_url=SPECS, brand_id=brand.id, raw_product_name="MacBook Pro",
                           raw_specs_dump=specs_dump, processing_status="completed")
    buy = RawScrapLaptop(source_url=BUY, brand_id=brand.id, raw_product_name="MacBook Pro",
                         raw_specs_dump={"scraped_features": [], "buy_page": {
                             "url": BUY, "specs_url": SPECS, "configs": configs, "nano_texture": []}})
    session.add_all([specs, buy])
    session.commit()
    return buy


def _run(session, monkeypatch, brand, configs, *templates, specs_dump=TWO_SIZES):
    calls = _stub(monkeypatch, *(templates or (_template(),)))
    buy = _pages(session, brand, configs, specs_dump)
    return processor.process_raw_laptop_data(str(buy.id), session), buy, calls


def _rows(session, brand):
    return session.exec(processor.select(processor.Laptop).where(processor.Laptop.brand_id == brand.id)).all()


def test_new_config_is_built_by_code_and_inactive(session, apple, monkeypatch):
    result, _, calls = _run(session, monkeypatch, apple, [_config()])
    assert result["status"] == "success", result
    assert len(calls) == 2, "the specs page was not sent one size at a time"

    [row] = _rows(session, apple)
    assert (row.model_code, row.product_name) == (CODE, NAME)
    assert (row.processor_model, row.display_brightness_nits) == ("Apple M5 Pro (15-core)", 1000)
    assert (row.ram_gb, row.ssd_gb, row.price_rm) == (24, 1024, 10299.0), "the LLM's memory, storage or price was stored"
    assert row.status == "inactive", "a new buy-page row went live by itself"
    assert row.part_numbers == ["MGDN4ZP/A", "MGDR4ZP/A"]
    [history] = session.exec(processor.select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == row.id)).all()
    assert (history.price_rm, history.source) == (10299.0, "apple-buy-page")


def test_unsourced_price_is_overwritten_and_recorded(session, apple, monkeypatch):
    june = make_laptop(apple.id, model_code=CODE, product_name="June", processor_model="Apple M5 Pro (15-core)",
                       display_size_inch=14.2, price_rm=9999.0, materials_and_certifications="kept")
    session.add(june)
    session.commit()

    result, _, _ = _run(session, monkeypatch, apple, [_config()])
    assert result["variants_updated"] == 1, result
    session.refresh(june)
    assert (june.price_rm, june.status, june.product_name) == (10299.0, "active", NAME)
    assert june.materials_and_certifications == "kept", "an empty extraction overwrote a stored field"
    assert result["price_changes"] == [{"laptop_id": str(june.id), "model_code": CODE, "status": "active",
                                        "old": 9999.0, "new": 10299.0}]
    sources = session.exec(processor.select(LaptopPriceHistory.source)
                           .where(LaptopPriceHistory.laptop_id == june.id)).all()
    assert sources == ["apple-buy-page"]


def test_guide_price_is_kept(session, apple, monkeypatch):
    row = make_laptop(apple.id, model_code=CODE, processor_model="Apple M5 Pro (15-core)",
                      display_size_inch=14.2, price_rm=9999.0)
    session.add(row)
    session.commit()
    session.add(LaptopPriceHistory(laptop_id=row.id, price_rm=9999.0, source="guide"))
    session.commit()

    result, _, _ = _run(session, monkeypatch, apple, [_config()])
    session.refresh(row)
    assert row.price_rm == 9999.0, "a guide price was overwritten"
    assert result["prices_not_overwritten"][0]["source"] == "guide"


def test_suspended_match_and_rows_off_the_page_are_untouched(session, apple, monkeypatch):
    suspended = make_laptop(apple.id, model_code=CODE, product_name="Retired", status="suspended",
                            processor_model="Apple M5 Pro (15-core)", display_size_inch=14.2)
    cto = make_laptop(apple.id, model_code=CODE.replace("24gb", "48gb"), product_name="Build to order",
                      status="inactive", processor_model="Apple M5 Pro (15-core)", display_size_inch=14.2)
    session.add_all([suspended, cto])
    session.commit()

    result, _, _ = _run(session, monkeypatch, apple, [_config()])
    session.refresh(suspended)
    session.refresh(cto)
    assert (suspended.product_name, suspended.status) == ("Retired", "suspended")
    assert (cto.product_name, cto.status) == ("Build to order", "inactive")
    assert result["matched_suspended"] == [{"laptop_id": str(suspended.id), "model_code": CODE}]
    assert [r["laptop_id"] for r in result["not_on_buy_page"]] == [str(cto.id)]
    assert result["variants_saved"] == result["variants_updated"] == 0


def test_identity_conflict_keeps_row(session, apple, monkeypatch):
    other = make_laptop(apple.id, model_code=CODE, product_name="16-inch", processor_model="Apple M5 Pro (15-core)",
                        display_size_inch=16.2)
    session.add(other)
    session.commit()
    result, _, _ = _run(session, monkeypatch, apple, [_config()])
    session.refresh(other)
    assert other.product_name == "16-inch"
    assert result["identity_conflicts"][0]["laptop_id"] == str(other.id)


def test_specs_page_alone_writes_nothing(session, apple, monkeypatch):
    """A specs page would let the LLM choose configurations: it is read only by its buy page."""
    calls = _stub(monkeypatch, _template())
    specs = RawScrapLaptop(source_url=SPECS, brand_id=apple.id, raw_product_name="MacBook Pro", raw_specs_dump=TWO_SIZES)
    session.add(specs)
    session.commit()
    result = processor.process_raw_laptop_data(str(specs.id), session)
    assert result["status"] == "success" and calls == [] and _rows(session, apple) == []


def test_specs_page_that_does_not_split_fails_the_buy_page(session, apple, monkeypatch):
    one = {"scraped_features": ["Product image" + _size(14)]}
    result, buy, calls = _run(session, monkeypatch, apple, [_config()], specs_dump=one)
    assert result["status"] == "skipped" and calls == []
    assert "expected 2 size sections" in result["split_failures"][0]["reason"]
    session.refresh(buy)
    assert buy.processing_status == "failed"


def test_bulk_job_result_lists_what_was_left_alone(session, apple, monkeypatch):
    """The lists reach the job's result, not only the per-record answer."""
    cto = make_laptop(apple.id, model_code=CODE.replace("24gb", "48gb"), status="inactive",
                      processor_model="Apple M5 Pro (15-core)", display_size_inch=14.2)
    session.add(cto)
    session.commit()
    _stub(monkeypatch, _template())
    monkeypatch.setattr(processor.time, "sleep", lambda _s: None)
    _pages(session, apple, [_config()])
    result = processor.process_pending_laptops(session)
    assert [r["laptop_id"] for r in result["not_on_buy_page"]] == [str(cto.id)]
    assert result["matched_suspended"] == [] and "price_changes" in result


_LR_PAGE = {"scraped_features": [(Path(__file__).parents[1] / "unit" / "fixtures" / "scraper"
                                  / "apple_macbook_pro_liquid_retina_specs.txt").read_text(encoding="utf-8")]}


def test_code_parsed_fields_replace_the_llm_values(session, apple, monkeypatch):
    """10a: max RAM, charger, weight, displays and resolution come from the page text, not
    from what the LLM said (the stub says max 128 and 70W, the 5c87abdd mistake)."""
    template = _template(processor_model="Apple M5 Max chip", cpu_cores=18, gpu_cores=32, max_ram_gb=128,
                         power_supply_details="70W USB-C Power Adapter", display_resolution="3024-by-1964")
    result, _, _ = _run(session, monkeypatch, apple, [_config(chip="M5 Max", cpu_cores=18, gpu_cores=32, ram_gb=36,
                                                              ssd_gb=2048)], template, specs_dump=_LR_PAGE)
    assert result["status"] == "success" and result["field_parse_failures"] == [], result
    [row] = _rows(session, apple)
    assert (row.max_ram_gb, row.power_supply_details) == (36, "96W USB-C Power Adapter")
    assert (row.weight_kg, row.display_resolution) == (1.62, "3024x1964")


def test_failed_parse_is_null_and_listed_not_the_stored_value(session, apple, monkeypatch):
    """A failed field is null even on update: keeping the stored (LLM-written) value would
    be the fallback 10a rules out."""
    june = make_laptop(apple.id, model_code=CODE, processor_model="Apple M5 Pro (15-core)", display_size_inch=14.2,
                       power_supply_details="70W USB-C Power Adapter", max_ram_gb=48, weight_kg=1.6)
    session.add(june)
    session.commit()
    result, _, _ = _run(session, monkeypatch, apple, [_config()])  # TWO_SIZES has none of the headings
    session.refresh(june)
    assert (june.power_supply_details, june.max_ram_gb, june.weight_kg) == (None, None, 0.0)
    [entry] = result["field_parse_failures"]
    assert entry["model_code"] == CODE
    assert {f["field"] for f in entry["failures"]} == {"max_ram_gb", "weight_kg", "power_supply_details",
                                                       "external_display_support", "display_resolution"}
