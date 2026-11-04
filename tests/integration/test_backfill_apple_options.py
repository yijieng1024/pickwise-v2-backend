"""
The Apple options backfill's --apply path (app/scripts/backfill_apple_options.py, step 10c),
run against the test database. Repair scripts get a test like this one since step (f)
of 10b: its --apply failed on a flush that only a real database could reach. Apple is
stubbed with real 2026-11-04 responses (tests/_apple_fake.py).
"""
import json

import pytest
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.laptops.option_model import LaptopOption
from app.scraper import apple_options_fetch as fetch
from app.scraper.models import RawScrapLaptop
from app.scripts import backfill_apple_options as backfill
from tests._apple_fake import AIR13_1010_SLUG, AIR13_SLUG, AIR_BUY, AIR_SPECS, FakeApple, fixture

pytestmark = pytest.mark.integration

NEO_BUY = "https://www.apple.com/my/shop/buy-mac/macbook-neo"


@pytest.fixture
def air13(session):
    apple = LaptopBrand(name="Apple", base_scrape_url="https://www.apple.com/my/mac/")
    session.add(apple)
    session.flush()

    def raw(url, name, dump):
        session.add(RawScrapLaptop(source_url=url, brand_id=apple.id, raw_product_name=name, raw_prices=[],
                                   raw_specs_dump=dump))

    raw(AIR_BUY, "Buy MacBook Air", {"buy_page": {"url": AIR_BUY, "specs_url": AIR_SPECS, "configs": [
        {"size": 13, "chip": "M5", "cpu_cores": 10, "gpu_cores": 8, "ram_gb": 16, "ssd_gb": 512,
         "slugs": [AIR13_SLUG], "part_numbers": ["MDH74ZP/A"]},
        {"size": 13, "chip": "M5", "cpu_cores": 10, "gpu_cores": 10, "ram_gb": 16, "ssd_gb": 1024,
         "slugs": [AIR13_1010_SLUG], "part_numbers": ["MDHA4ZP/A"]}]}})
    raw(AIR_SPECS, "MacBook Air", {"scraped_features": [fixture("apple_macbook_air_specs.txt")]})
    # No build-to-order: neither fetched nor failed.
    raw(NEO_BUY, "Buy MacBook Neo", {"buy_page": {"url": NEO_BUY, "specs_url": "https://www.apple.com/my/macbook-neo/specs/",
                                                  "configs": [{"size": 13, "ssd_gb": 256, "slugs": ["silver-256gb"],
                                                               "part_numbers": ["MHFJ4ZP/A"]}]}})
    session.commit()


def _rows(session):
    return session.exec(select(LaptopOption)).all()


def test_apply_writes_each_chip_kit_and_a_rerun_replaces_them(session, air13, tmp_path):
    result = backfill.run(session, write=True, get=FakeApple(), pause_s=0, backups=tmp_path)

    assert result["chip_kits"] == 2 and result["options_failed"] == [] and result["stopped"] is None
    assert result["apple_calls"] == 4, "the Air's 2 configure pages + 2 update-config; nothing for the Neo"
    rows = _rows(session)
    chips = {r.chip: r for r in rows if r.option_group == "chip"}
    assert {c: r.kit_price_rm for c, r in chips.items()} == {"10-8": 5499.0, "10-10": 6849.0}
    assert chips["10-10"].cpu_model == "Apple M5 (10-core)"
    by_key = {(r.chip, r.option_group, r.value): r for r in rows}
    assert by_key[("10-10", "memory", "24gb")].add_on_rm == 900.0
    assert ("10-8", "memory", "24gb") not in by_key
    assert {(r.product, r.size_inch, r.line) for r in rows} == {("MacBook Air", 13, "")}

    pre, post = sorted(tmp_path.glob("*.json"))
    assert json.loads(pre.read_text())["laptop_options"] == []
    assert len(json.loads(post.read_text())["laptop_options"]) == len(rows)

    backfill.run(session, write=True, get=FakeApple(), pause_s=0, backups=tmp_path)
    assert len(_rows(session)) == len(rows)


def test_a_failed_kit_keeps_its_stored_rows(session, air13, tmp_path):
    backfill.run(session, write=True, get=FakeApple(), pause_s=0, backups=tmp_path)
    stored = len(_rows(session))

    broken = FakeApple()
    broken.update_config["10-10"] = "<html>Access Denied</html>"
    result = backfill.run(session, write=True, get=broken, pause_s=0, backups=tmp_path)
    assert [f["chip"] for f in result["options_failed"]] == ["10-10"]
    assert len(_rows(session)) == stored
    assert any(r.chip == "10-10" and r.option_group == "chip" for r in _rows(session))


def test_proled_and_liquid_retina_kits_of_one_chip_are_stored_separately(session):
    def kit(line, price):
        return {"product": "MacBook Pro", "size_inch": 14, "line": line, "chip": "m5pro-15-16",
                "rows": [{"option_group": "chip", "value": "m5pro-15-16", "label": None, "add_on_rm": None,
                          "kit_price_rm": price, "is_included": True, "is_blocked": False,
                          "cpu_model": None, "gpu_model": None}]}

    fetch.write(session, [kit("proled", 11999.0), kit("liquid-retina", 10299.0)])
    fetch.write(session, [kit("liquid-retina", 10299.0)])  # a rerun of one line leaves the other
    prices = {r.line: r.kit_price_rm for r in _rows(session)}
    assert prices == {"proled": 11999.0, "liquid-retina": 10299.0}
