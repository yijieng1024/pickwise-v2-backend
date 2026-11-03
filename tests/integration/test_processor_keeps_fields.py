"""
Re-processing must not erase what an extraction failed to find. None, "", [] and {} are
"not found" (as price 0 is "price unknown"), so they never overwrite a stored value, and
the job result says which fields were kept.
"""
import pytest
from langchain_core.runnables import RunnableLambda

from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def test_empty_extraction_keeps_the_stored_value(session, brand, monkeypatch):
    stored = make_laptop(brand.id, model_code="keeps-fields", camera_details="1080p FHD camera",
                         audio_details="Dolby Atmos", colors=["Black"], ports_summary=["2x USB-C"],
                         wifi_standard="Wi-Fi 6E", price_rm=4999.0)
    raw = RawScrapLaptop(source_url="https://example.invalid/p", brand_id=brand.id, raw_product_name="Test Laptop")
    session.add_all([stored, raw])
    session.commit()

    fields = {name: getattr(stored, name, None) for name in ExtractedLaptopVariant.model_fields}
    fields.update(categories=[], unmapped_specs={"test": True}, camera_details=None, audio_details="  ",
                  colors=[], ports_summary=["2x USB-C", "HDMI"], wifi_standard="Wi-Fi 7")
    extracted = ExtractedLaptopFamily.model_construct(variants=[ExtractedLaptopVariant.model_construct(**fields)])

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result

    session.refresh(stored)
    assert (stored.camera_details, stored.audio_details, stored.colors) == ("1080p FHD camera", "Dolby Atmos", ["Black"]), (
        "an empty extraction erased a stored value"
    )
    # A real new value still overwrites: the fix is not "never update"
    assert (stored.ports_summary, stored.wifi_standard) == (["2x USB-C", "HDMI"], "Wi-Fi 7")
    [kept] = result["fields_kept"]
    assert kept["laptop_id"] == str(stored.id)
    assert set(kept["fields"]) >= {"camera_details", "audio_details", "colors"}
    assert not {"ports_summary", "wifi_standard"} & set(kept["fields"])
