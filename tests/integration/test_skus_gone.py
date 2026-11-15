"""The weekly email's "SKUs no longer on their page" against a real database: last_seen_at
is stamped for a SKU seen (-WP seen as -W), a laptop none of whose SKUs were seen is
listed, and nothing changes a status."""
from datetime import datetime, timezone

import pytest

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import LaptopSku
from app.scripts import weekly_pipeline as wp
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)


def test_seen_skus_are_stamped_and_a_gone_one_is_listed(session):
    asus = LaptopBrand(name="Asus")
    session.add(asus)
    session.commit()
    seen = make_laptop(asus.id, product_name="ROG Strix G16 (G614P-MTS088WP)")
    gone = make_laptop(asus.id, product_name="ROG Strix G16 (G614P-WRV068W)")
    tuf = make_laptop(asus.id, product_name="ASUS TUF Gaming A16", manufacturer_sku="FA608UH-RV001W")
    session.add_all([seen, gone, tuf])
    session.commit()
    session.add_all([LaptopSku(laptop_id=seen.id, sku="G614P-MTS088WP"), LaptopSku(laptop_id=gone.id, sku="G614P-WRV068W")])
    session.commit()

    steps = {"scrape": {"asus": {"skus_seen": ["G614P-MTS088W"], "failed_pages": []}}}
    listed = wp.skus_gone(session, steps, {"asus": ["rog.asus.com", "www.asus.com"]}, NOW)

    assert [g["laptop_id"] for g in listed] == [str(gone.id)], "the TUF row's pages are not re-scraped"
    assert listed[0]["last_seen"] is None
    stamps = {r.sku: r.last_seen_at for r in session.exec(wp.select(LaptopSku)).all()}
    assert stamps["G614P-MTS088WP"] == NOW and stamps["G614P-WRV068W"] is None
    session.refresh(gone)
    assert gone.status == "active"
