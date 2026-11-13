"""
The processing result records what each raw row did: `writes` has the raw row id,
the laptop it wrote to, and inserted, updated or skipped, with the old and new
price of an update that changed it (plan step 11, commit 4). The go-live check
and the summary email read it.
"""
import pytest
from langchain_core.runnables import RunnableLambda
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def _variant(laptop, **over):
    fields = {name: getattr(laptop, name, None) for name in ExtractedLaptopVariant.model_fields}
    fields.update(categories=[], unmapped_specs={"test": True}, **over)
    return ExtractedLaptopVariant.model_construct(**fields)


def _stub(monkeypatch, *variants):
    extracted = ExtractedLaptopFamily.model_construct(variants=list(variants))

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())


def test_writes_lists_each_laptop_a_raw_row_wrote(session, brand, monkeypatch):
    repriced = make_laptop(brand.id, model_code="writes-repriced", price_rm=5999.0)
    same = make_laptop(brand.id, model_code="writes-same", price_rm=4000.0)
    raw = RawScrapLaptop(source_url="https://example.invalid/w", brand_id=brand.id, raw_product_name="Test Laptop")
    session.add_all([repriced, same, raw])
    session.commit()
    _stub(monkeypatch, _variant(repriced, price_rm=5499.0), _variant(same),
          _variant(same, model_code="writes-new", price_rm=3000.0))

    result = processor.process_raw_laptop_data(str(raw.id), session)

    assert result["status"] == "success", result
    new = session.exec(select(Laptop).where(Laptop.model_code == "writes-new")).one()
    assert result["writes"] == [
        {"raw_id": str(raw.id), "laptop_id": str(repriced.id), "action": "updated",
         "old_price": 5999.0, "new_price": 5499.0},
        {"raw_id": str(raw.id), "laptop_id": str(same.id), "action": "updated"},
        {"raw_id": str(raw.id), "laptop_id": str(new.id), "action": "inserted"},
    ]


def test_a_row_that_wrote_no_laptop_is_skipped_and_the_job_result_has_every_row(session, brand, monkeypatch):
    apple = LaptopBrand(name="Apple", base_scrape_url="https://example.invalid/apple")
    session.add(apple)
    session.commit()
    # An Apple specs page writes nothing itself: its buy page's run reads it.
    specs = RawScrapLaptop(source_url="https://example.invalid/specs", brand_id=apple.id,
                           raw_product_name="MacBook Pro", raw_specs_dump={"scraped_features": []})
    raw = RawScrapLaptop(source_url="https://example.invalid/n", brand_id=brand.id, raw_product_name="Test Laptop")
    session.add_all([specs, raw])
    session.commit()
    _stub(monkeypatch, _variant(make_laptop(brand.id, model_code="writes-job-new")))

    report = processor.process_pending_laptops(session)

    new = session.exec(select(Laptop).where(Laptop.model_code == "writes-job-new")).one()
    assert sorted(report["writes"], key=lambda w: w["action"]) == [
        {"raw_id": str(raw.id), "laptop_id": str(new.id), "action": "inserted"},
        {"raw_id": str(specs.id), "laptop_id": None, "action": "skipped"},
    ]
    by_raw = {d["raw_id"]: d["writes"] for d in report["details"]}
    assert by_raw[str(specs.id)] == [{"raw_id": str(specs.id), "laptop_id": None, "action": "skipped"}]
