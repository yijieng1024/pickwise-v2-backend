"""
What the processor stores when the LLM invents a model code.

Catches the name reaching `laptops` unchecked: raw row "(S5406SAL)" came out of
Gemma as "(S5406M)", and nothing compared the two. The check itself is unit
tested; this is about the write and the run's record of it.
"""
import pytest
from sqlmodel import select

from app.laptops.laptop_models import Laptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def test_an_invented_code_is_stored_as_the_raw_code_and_listed_in_the_run(session, brand, monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from app.processor import engine as processor
    from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
    from app.scraper.models import RawScrapLaptop

    raw = RawScrapLaptop(source_url="https://example.invalid/s5406", brand_id=brand.id,
                         raw_product_name="ASUS Vivobook S 14 (S5406) (S5406SAL)",
                         raw_specs_dump={"scraped_features": {"Processor": "Intel Core Ultra 5 226V"}})
    session.add(raw)
    session.commit()

    template = make_laptop(brand.id, model_code="asus-vivobook-s-14-s5406", price_rm=5099.0)
    fields = {name: getattr(template, name, None) for name in ExtractedLaptopVariant.model_fields}
    fields.update(categories=[], unmapped_specs={"test": True},
                  product_name="ASUS Vivobook S 14 (S5406M) (Intel Core Ultra 5 226V)")
    extracted = ExtractedLaptopFamily.model_construct(
        variants=[ExtractedLaptopVariant.model_construct(**fields)]
    )

    class _StubLLM:
        def __init__(self, **_):
            pass

        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())

    report = processor.process_pending_laptops(session, limit=1)

    stored = session.exec(select(Laptop).where(Laptop.model_code == "asus-vivobook-s-14-s5406")).one()
    assert stored.product_name == "ASUS Vivobook S 14 (S5406SAL) (Intel Core Ultra 5 226V)"
    session.refresh(raw)
    assert raw.processing_status == "completed", "a swap must never fail the record"
    assert report["model_codes_replaced"] == [{
        "raw_id": str(raw.id),
        "invented": ["S5406M"],
        "llm_name": "ASUS Vivobook S 14 (S5406M) (Intel Core Ultra 5 226V)",
        "product_name": stored.product_name,
    }]
    assert report["details"][0]["model_codes_replaced"] == report["model_codes_replaced"]
