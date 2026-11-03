"""
The Apple upsert (app/processor/engine.py + apple_name.py).

On 2026-11-03 the PROLED XDR page's 14" M5 Pro 24GB/2TB got the same slug as a June
Liquid Retina XDR row, and the processor overwrote that row with the new machine:
name, display, weight, battery, images, all of it, keeping the June price. These
tests pin the three guards: the line in the code, the name built from specs, and a
code match on a different machine left alone. The LLM is stubbed throughout.
"""
import pytest
from langchain_core.runnables import RunnableLambda

from app.laptops.brand_model import LaptopBrand
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

PROLED = "https://www.apple.com/my/macbook-pro/proled-xdr-display/specs/"
SLUG = "apple-macbook-pro-14-m5-pro-15c-16g-24gb-2048gb"


@pytest.fixture
def apple(session) -> LaptopBrand:
    row = LaptopBrand(name="Apple", base_scrape_url="https://example.invalid/apple")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _variant(**fields) -> ExtractedLaptopVariant:
    base = {name: None for name in ExtractedLaptopVariant.model_fields}
    base.update(
        model_code=SLUG, product_name="MacBook Pro with PROLED XDR Display", price_rm=0.0,
        processor_model="Apple M5 Pro chip", gpu_model="16-core GPU", cpu_cores=15, gpu_cores=16, ram_gb=24,
        weight_kg=1.38, battery_wh=72.6,
        ssd_gb=2048, display_size_inch=14.2, display_type="PROLED XDR display",
        categories=[], unmapped_specs={"test": True}, colors=[], ports_summary=[], ai_features=[],
    )
    base.update(fields)
    return ExtractedLaptopVariant.model_construct(**base)


def _size(inch: int) -> str:
    return (f"\nFinish\nSilver\n\nChip\nApple M5 Pro chip\n\nSize and Weight\nWidth: {inch}cm\n"
            f"\nOperating System\nmacOS\n\nIn the Box\n{inch}-inch MacBook Pro\n\nEnvironment\n")


# A MacBook Pro page as the scraper stores it: two size sections.
TWO_SIZES = {"scraped_features": ["Product image" + _size(14) + _size(16)]}


def _run(session, monkeypatch, brand, url, *variants, dump=TWO_SIZES):
    """The stub answers the first size section with *variants* and the second with none,
    and records the specs text of every call."""
    calls = []

    class _StubLLM:
        def with_structured_output(self, _schema):
            def extract(inputs):
                calls.append(inputs.to_string())
                return ExtractedLaptopFamily.model_construct(variants=list(variants) if len(calls) == 1 else [])
            return RunnableLambda(extract)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    raw = RawScrapLaptop(source_url=url, brand_id=brand.id, raw_product_name="MacBook Pro", raw_specs_dump=dump)
    session.add(raw)
    session.commit()
    return processor.process_raw_laptop_data(str(raw.id), session), raw, calls


def test_apple_name_ignores_llm(session, apple, monkeypatch):
    """The stored name, code, processor and brightness come from the specs and URL, not
    the LLM's wording."""
    result, _, _ = _run(session, monkeypatch, apple, PROLED, _variant(display_brightness_nits=1600))
    assert result["status"] == "success", result

    stored = session.exec(
        processor.select(processor.Laptop).where(processor.Laptop.brand_id == apple.id)
    ).one()
    assert stored.product_name == (
        "14-inch MacBook Pro PROLED XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 2TB SSD)"
    )
    assert stored.model_code == "apple-macbook-pro-14-proled-m5-pro-15c-16g-24gb-2048gb"
    assert (stored.processor_model, stored.display_brightness_nits) == ("Apple M5 Pro (15-core)", 2000), (
        "the LLM's processor wording or brightness was stored"
    )


def test_identity_conflict_keeps_row(session, apple, monkeypatch):
    """
    A code match whose screen size differs is another machine: the row is not
    touched, and the job result names it.
    """
    code = "apple-macbook-pro-14-proled-m5-pro-15c-16g-24gb-2048gb"
    june = make_laptop(apple.id, model_code=code, product_name="June row",
                       processor_model="Apple M5 Pro (15-core)", display_size_inch=16.2,
                       weight_kg=2.14, price_rm=12549.0)
    session.add(june)
    session.commit()

    result, raw, _ = _run(session, monkeypatch, apple, PROLED, _variant(weight_kg=1.38))
    assert result["status"] == "success", result

    session.refresh(june)
    assert (june.product_name, june.display_size_inch, june.weight_kg) == ("June row", 16.2, 2.14), (
        "a code match on a different machine overwrote the row"
    )
    assert result["variants_updated"] == 0
    [conflict] = result["identity_conflicts"]
    assert conflict["laptop_id"] == str(june.id)
    assert conflict["model_code"] == code
    assert conflict["existing"]["screen_inch"] == 16 and conflict["incoming"]["screen_inch"] == 14
    assert (conflict["raw_id"], conflict["source_url"]) == (str(raw.id), PROLED)


def test_unknown_macbook_pro_url_is_skipped_and_listed(session, apple, monkeypatch):
    """No default line: an unknown MacBook Pro page writes nothing, costs no LLM
    call, and is listed for an admin."""
    url = "https://www.apple.com/my/macbook-pro/nano-texture/specs/"
    result, raw, calls = _run(session, monkeypatch, apple, url, _variant())

    assert result["status"] == "skipped", result
    assert calls == [], "the LLM was called for a page that cannot be placed"
    assert session.exec(
        processor.select(processor.Laptop).where(processor.Laptop.brand_id == apple.id)
    ).all() == []
    [conflict] = result["identity_conflicts"]
    assert (conflict["raw_id"], conflict["source_url"]) == (str(raw.id), url)
    session.refresh(raw)
    assert raw.processing_status == "failed"


def test_apple_page_is_extracted_one_size_at_a_time(session, apple, monkeypatch):
    """The 16-inch half of the PROLED page lay past the 8,000-character cut and never
    reached the LLM. Each size now gets its own call, with only its own section."""
    result, _, calls = _run(session, monkeypatch, apple, PROLED, _variant())
    assert result["status"] == "success", result
    assert len(calls) == 2, "the page was not sent one size at a time"
    assert "Width: 14cm" in calls[0] and "Width: 16cm" not in calls[0]
    assert "Width: 16cm" in calls[1] and "Width: 14cm" not in calls[1]
    assert "Environment" not in calls[0] + calls[1], "the copy after the specs was sent"


def test_apple_page_that_does_not_split_fails_and_is_listed(session, apple, monkeypatch):
    """A MacBook Pro page with one size section is a layout change, not a one-size
    page: nothing is written, no LLM call, the row fails and the result says why."""
    one = {"scraped_features": ["Product image" + _size(14)]}
    result, raw, calls = _run(session, monkeypatch, apple, PROLED, _variant(), dump=one)

    assert result["status"] == "skipped", result
    assert calls == []
    [failure] = result["split_failures"]
    assert (failure["raw_id"], failure["source_url"]) == (str(raw.id), PROLED)
    assert "expected 2 size sections" in failure["reason"]
    session.refresh(raw)
    assert raw.processing_status == "failed"
