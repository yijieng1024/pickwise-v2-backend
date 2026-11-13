"""
New laptops go live only through the go-live check (plan step 11, decisions 3 to 5).

The processor inserts every new laptop inactive and held. The check, at the end of
every processing job, activates the held ones that pass and leaves the rest held with
their failed conditions. It never touches a laptop without the marker. The conditions
themselves are unit tested (tests/unit/test_go_live.py).
"""
import pytest
from langchain_core.runnables import RunnableLambda
from sqlmodel import select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.go_live import run_go_live_check
from app.laptops.laptop_models import Laptop
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def _benchmarks(session):
    """The marks make_laptop's CPU and GPU resolve to."""
    session.add(CPUBenchmark(cpu_name="Intel Core i7-14650HX", cpu_mark=33467))
    session.add(GPUBenchmark(gpu_name="GeForce RTX 5060 Laptop GPU", gpu_mark=15000))
    session.commit()


def _process(session, brand, monkeypatch, **variant):
    """One raw row through a processing job; the stubbed LLM extracts one variant."""
    template = make_laptop(brand.id)
    fields = {name: getattr(template, name, None) for name in ExtractedLaptopVariant.model_fields}
    fields.update(categories=[], unmapped_specs={"test": True}, **variant)
    extracted = ExtractedLaptopFamily.model_construct(variants=[ExtractedLaptopVariant.model_construct(**fields)])

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    session.add(RawScrapLaptop(source_url=f"https://example.invalid/{variant['model_code']}", brand_id=brand.id,
                               raw_product_name="Test Laptop"))
    session.commit()
    report = processor.process_pending_laptops(session)
    return report, session.exec(select(Laptop).where(Laptop.model_code == variant["model_code"])).one()


def test_a_new_laptop_that_passes_becomes_active_and_loses_the_marker(session, brand, monkeypatch):
    _benchmarks(session)
    report, laptop = _process(session, brand, monkeypatch, model_code="go-live-pass")

    assert (laptop.status, laptop.go_live_held, laptop.go_live_failures) == ("active", False, None)
    entry = {"laptop_id": str(laptop.id), "model_code": "go-live-pass", "product_name": "Test Laptop"}
    assert report["go_live"] == {"checked": 1, "activated": [entry], "held": [],
                                 "no_family": [entry]}, "a null family is reported and does not block"


def test_a_new_laptop_that_fails_stays_inactive_with_the_reason_on_it(session, brand, monkeypatch):
    # No benchmark rows: neither part resolves.
    report, laptop = _process(session, brand, monkeypatch, model_code="go-live-fail")

    failed = ["cpu_benchmark", "gpu_benchmark"]
    assert (laptop.status, laptop.go_live_held, laptop.go_live_failures) == ("inactive", True, failed)
    assert report["go_live"]["activated"] == []
    assert [(h["laptop_id"], h["failed"]) for h in report["go_live"]["held"]] == [(str(laptop.id), failed)]

    # Decision 5: what it waited for arrives, and the next check activates it by itself.
    _benchmarks(session)
    assert [a["laptop_id"] for a in run_go_live_check(session)["activated"]] == [str(laptop.id)]
    session.refresh(laptop)
    assert (laptop.status, laptop.go_live_held, laptop.go_live_failures) == ("active", False, None)


def test_a_laptop_set_inactive_by_hand_is_never_touched(session, brand):
    _benchmarks(session)
    by_hand = make_laptop(brand.id, status="inactive")  # passes every condition, has no marker
    session.add(by_hand)
    session.commit()

    assert run_go_live_check(session) == {"checked": 0, "activated": [], "held": [], "no_family": []}
    session.refresh(by_hand)
    assert (by_hand.status, by_hand.go_live_held, by_hand.go_live_failures) == ("inactive", False, None)


def test_an_update_of_an_existing_laptop_does_not_set_the_marker(session, brand, monkeypatch):
    existing = make_laptop(brand.id, model_code="go-live-existing", status="inactive", price_rm=4000.0)
    session.add(existing)
    session.commit()
    _benchmarks(session)

    report, laptop = _process(session, brand, monkeypatch, model_code="go-live-existing", price_rm=4500.0)

    assert laptop.id == existing.id and laptop.price_rm == 4500.0, "the row was not updated"
    assert (laptop.status, laptop.go_live_held) == ("inactive", False)
    assert report["go_live"]["checked"] == 0


def test_an_admin_status_change_takes_a_held_laptop_from_the_check(session, brand, admin_client):
    _benchmarks(session)
    held = make_laptop(brand.id, status="inactive", go_live_held=True, go_live_failures=["price"])
    session.add(held)
    session.commit()

    # Same status sent again (the admin form sends every field): still held.
    assert admin_client.put(f"/api/v2/laptops/{held.id}", json={"status": "inactive"}).status_code == 200
    session.refresh(held)
    assert held.go_live_held is True

    response = admin_client.put(f"/api/v2/laptops/{held.id}", json={"status": "suspended"})
    assert response.status_code == 200, response.text
    assert (response.json()["go_live_held"], response.json()["go_live_failures"]) == (False, None)
    admin_client.put(f"/api/v2/laptops/{held.id}", json={"status": "inactive"})
    assert run_go_live_check(session)["checked"] == 0, "the check activated a laptop the admin set inactive"
