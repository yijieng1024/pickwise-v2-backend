"""
The Apple options backfill's --apply path (app/scripts/backfill_apple_options.py, step 10c),
run against the test database. Repair scripts get a test like this one since step (f)
of 10b: its --apply failed on a flush that only a real database could reach. Apple is
stubbed with the real trial responses (tests/_apple_fake.py).
"""
import json

import pytest
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import LaptopStatus
from app.laptops.option_model import LaptopOption, LaptopOptionRule
from app.scraper.models import RawScrapLaptop
from app.scripts import backfill_apple_options as backfill
from tests._apple_fake import AIR13_SLUG, AIR_BUY, AIR_SPECS, FakeApple, fixture
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

PART = "MDH74ZP/A"


@pytest.fixture
def air13(session):
    apple = LaptopBrand(name="Apple", base_scrape_url="https://www.apple.com/my/mac/")
    session.add(apple)
    session.flush()
    session.add(RawScrapLaptop(
        source_url=AIR_BUY, brand_id=apple.id, raw_product_name="Buy MacBook Air", raw_prices=[],
        raw_specs_dump={"buy_page": {"url": AIR_BUY, "specs_url": AIR_SPECS, "configs": [
            {"size": 13, "chip": "M5", "cpu_cores": 10, "gpu_cores": 8, "ram_gb": 16, "ssd_gb": 512,
             "slugs": [AIR13_SLUG], "part_numbers": [PART]}]}},
    ))
    session.add(RawScrapLaptop(
        source_url=AIR_SPECS, brand_id=apple.id, raw_product_name="MacBook Air", raw_prices=[],
        raw_specs_dump={"scraped_features": [fixture("apple_macbook_air_specs.txt")]},
    ))
    laptop = make_laptop(apple.id, model_code="apple-macbook-air-13-m5-10c-8g-16gb-512gb",
                         product_name="MacBook Air 13", processor_model="Apple M5 (10-core)",
                         gpu_model="8-core GPU", display_size_inch=13.6, part_numbers=[PART])
    # Not fetched: a build-to-order row (no part numbers) and a suspended standard config.
    bto = make_laptop(apple.id, part_numbers=None)
    gone = make_laptop(apple.id, part_numbers=["MXXX4ZP/A"], status=LaptopStatus.SUSPENDED.value)
    session.add_all([laptop, bto, gone])
    session.commit()
    return laptop


def test_apply_writes_options_and_rules_and_a_rerun_replaces_them(session, air13, tmp_path):
    fake = FakeApple()
    result = backfill.run(session, write=True, get=fake, pause_s=0, backups=tmp_path)

    assert result["machines"] == 1 and result["options_failed"] == [] and result["stopped"] is None
    assert result["apple_calls"] == 8
    options = session.exec(select(LaptopOption).where(LaptopOption.laptop_id == air13.id)).all()
    by_key = {(o.option_group, o.value): o for o in options}
    assert len(options) == 18
    assert by_key[("memory", "24gb")].add_on_rm == 900.0 and by_key[("memory", "16gb")].is_included
    assert by_key[("chip", "10-10")].cpu_model == "Apple M5 (10-core)"
    rules = session.exec(select(LaptopOptionRule).where(LaptopOptionRule.laptop_id == air13.id)).all()
    assert {(r.trigger_value, r.forced_group, r.forced_value) for r in rules} == {
        ("24gb", "chip", "10-10"), ("32gb", "chip", "10-10"),
        ("1tb", "chip", "10-10"), ("2tb", "chip", "10-10"), ("4tb", "chip", "10-10")}
    assert session.exec(select(LaptopOption).where(LaptopOption.laptop_id != air13.id)).all() == []

    pre, post = sorted(tmp_path.glob("*.json"))
    assert json.loads(pre.read_text())["laptop_options"] == []
    assert len(json.loads(post.read_text())["laptop_options"]) == 18

    backfill.run(session, write=True, get=FakeApple(), pause_s=0, backups=tmp_path)
    assert len(session.exec(select(LaptopOption).where(LaptopOption.laptop_id == air13.id)).all()) == 18
    assert len(session.exec(select(LaptopOptionRule).where(LaptopOptionRule.laptop_id == air13.id)).all()) == 5


def test_a_failed_machine_keeps_its_stored_options(session, air13, tmp_path):
    backfill.run(session, write=True, get=FakeApple(), pause_s=0, backups=tmp_path)

    def broken(url):
        return "<html>Access Denied</html>"

    result = backfill.run(session, write=True, get=broken, pause_s=0, backups=tmp_path)
    assert [f["model_code"] for f in result["options_failed"]] == [air13.model_code]
    assert len(session.exec(select(LaptopOption).where(LaptopOption.laptop_id == air13.id)).all()) == 18
