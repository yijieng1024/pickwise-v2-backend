"""
GET /laptops/{id}/options and /configure (step 10c, step (d)) against the test database,
with the Pro 14 Liquid Retina chip kits stored in production on 2026-11-04.
"""
import json
from pathlib import Path

import pytest

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import LaptopStatus
from app.laptops.option_model import LaptopOption
from app.laptops.pickscore_adapter import get_laptop_ranges
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

KITS = Path(__file__).parents[1] / "unit" / "fixtures" / "apple_kits_pro_2026-11-04.json"


@pytest.fixture
def m6(session):
    apple = LaptopBrand(name="Apple", base_scrape_url="https://www.apple.com/my/mac/")
    session.add(apple)
    session.flush()
    for row in json.loads(KITS.read_text(encoding="utf-8")):
        if row["size_inch"] == 14 and row["line"] == "liquid-retina":
            session.add(LaptopOption(**row))
    laptop = make_laptop(apple.id, model_code="apple-macbook-pro-14-liquid-retina-m6-12c-12g-16gb-1024gb",
                         product_name="MacBook Pro 14 (M6)", price_rm=8299.0, processor_model="Apple M6 (12-core)",
                         gpu_model="12-core GPU", cpu_cores=12, gpu_cores=12, ram_gb=16, ssd_gb=1024,
                         display_size_inch=14.2, part_numbers=["MHWT4ZP/A"])
    session.add(laptop)
    session.commit()
    return laptop


def test_options_lists_every_chip_kit_of_the_size_and_line(api_client, m6):
    resp = api_client.get(f"/api/v2/laptops/{m6.id}/options")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["standard_chip"] == "m6-12-12"
    assert body["standard"]["memory"] == "16gb" and body["standard"]["storage"] == "1tb"
    kits = {k["chip"]: k for k in body["kits"]}
    assert set(kits) == {"m6-12-12", "m5pro-15-16", "m5pro-18-20", "m5max-18-32", "m5max-18-40"}
    assert kits["m6-12-12"]["chip_add_on_rm"] == 0.0
    assert [o["value"] for o in kits["m5max-18-32"]["options"]["memory"]] == ["36gb"]


def test_configure_totals_apples_figure(api_client, m6):
    resp = api_client.get(f"/api/v2/laptops/{m6.id}/configure?memory=32gb&storage=4tb")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_rm"] == 16849.0, "Apple's own total for Pro 14 M6 + 32GB + 4TB"


def test_another_chip_is_priced_from_its_own_kit(api_client, m6):
    resp = api_client.get(f"/api/v2/laptops/{m6.id}/configure?chip=m5pro-15-16&memory=48gb&storage=2tb")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_rm"] == 15249.0, "Apple's own total for Pro 14 Liquid Retina M5 Pro + 48GB + 2TB"
    assert body["chip_add_on_rm"] == 10299.0 - 8299.0


def test_an_option_the_chip_does_not_allow_is_a_422_naming_the_chip_that_offers_it(api_client, m6):
    resp = api_client.get(f"/api/v2/laptops/{m6.id}/configure?chip=m5pro-15-16&memory=16gb")
    assert resp.status_code == 422
    [problem] = resp.json()["detail"]["not_allowed"]
    assert problem == {"group": "memory", "value": "16gb", "allowed": ["24gb", "48gb"], "offered_by": ["m6-12-12"]}
    assert api_client.get(f"/api/v2/laptops/{m6.id}/configure?chip=m9").status_code == 422


def test_only_active_apple_standard_configs_have_options(api_client, session, m6, brand):
    other = make_laptop(brand.id)
    session.add(other)
    m6.status = LaptopStatus.SUSPENDED.value
    session.add(m6)
    session.commit()
    assert api_client.get(f"/api/v2/laptops/{other.id}/options").status_code == 404
    assert api_client.get(f"/api/v2/laptops/{m6.id}/options").status_code == 404
    assert api_client.get(f"/api/v2/laptops/{m6.id}/configure").status_code == 404


def test_configuring_leaves_the_catalog_ranges_unchanged(api_client, session, m6):
    before = get_laptop_ranges(session, force_refresh=True)
    resp = api_client.get(f"/api/v2/laptops/{m6.id}/configure?chip=m5max-18-40&memory=128gb&storage=8tb")
    assert resp.status_code == 200, resp.text
    assert get_laptop_ranges(session, force_refresh=True) == before
