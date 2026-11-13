"""
The processor finds a laptop by the maker's SKU before it tries the slug, which is the
LLM's wording and can change between two scrapes of one page. ROG pages carry the SKU
as "SKU"; HP pages as "Product Info / Sku" (plan step 11, decision 6). Without the
SKU, a re-scraped page whose slug came out differently made a second laptop.
"""
import pytest
from langchain_core.runnables import RunnableLambda
from sqlmodel import select

from app.laptops.laptop_models import Laptop
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def _reprocess(session, brand, monkeypatch, features: dict, sku: str):
    """A laptop holding *sku*, then its page processed again with the slug worded anew."""
    stored = make_laptop(brand.id, model_code="first-wording", price_rm=4000.0)
    stored.manufacturer_sku = sku
    raw = RawScrapLaptop(source_url="https://example.invalid/p", brand_id=brand.id, raw_product_name="Test Laptop",
                         raw_specs_dump={"scraped_features": features})
    session.add_all([stored, raw])
    session.commit()

    fields = {name: getattr(stored, name, None) for name in ExtractedLaptopVariant.model_fields}
    fields.update(categories=[], unmapped_specs={"test": True}, model_code="second-wording", price_rm=4500.0)
    extracted = ExtractedLaptopFamily.model_construct(variants=[ExtractedLaptopVariant.model_construct(**fields)])

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result
    return stored, result, session.exec(select(Laptop).where(Laptop.brand_id == brand.id)).all()


def test_an_hp_row_finds_its_laptop_by_the_sku_under_product_info(session, brand, monkeypatch):
    stored, result, rows = _reprocess(session, brand, monkeypatch, {"Product Info / Sku": "DP7X3PA"}, "DP7X3PA")

    assert [r.id for r in rows] == [stored.id], "the re-scraped HP page made a second laptop"
    assert (rows[0].model_code, rows[0].price_rm) == ("first-wording", 4500.0)
    assert result["writes"] == [{"raw_id": result["writes"][0]["raw_id"], "laptop_id": str(stored.id),
                                 "action": "updated", "old_price": 4000.0, "new_price": 4500.0}]


def test_a_rog_row_still_finds_its_laptop_by_sku(session, brand, monkeypatch):
    stored, _, rows = _reprocess(session, brand, monkeypatch, {"SKU": "G835L-WGSA478WP"}, "G835L-WGSA478W")

    # -W and -WP read as one SKU, as before
    assert [r.id for r in rows] == [stored.id]
    assert (rows[0].model_code, rows[0].price_rm) == ("first-wording", 4500.0)
