"""SKUs no longer on their page (plan step 11, Jack 2026-11-15, option C): an active
laptop none of whose SKUs a run's scrape of the re-scraped sites saw. Notify only."""
from datetime import datetime, timezone

from tests.unit._adapters import pipeline_summary as summary
from tests.unit._adapters import weekly_pipeline as wp

LAST = datetime(2026, 11, 15, tzinfo=timezone.utc)


def _laptop(laptop_id, skus, last_seen=None, host="rog.asus.com"):
    return {"laptop_id": laptop_id, "model_code": f"code-{laptop_id}", "product_name": f"ROG {laptop_id}",
            "skus": skus, "last_seen": last_seen, "host": host}


def test_a_laptop_none_of_whose_skus_were_seen_is_listed():
    gone = wp.gone_laptops([_laptop("L1", ["G614P-WRV068W"], LAST)], {"G614P-MTS088W"}, set(), {"rog.asus.com"})
    assert [g["laptop_id"] for g in gone] == ["L1"] and gone[0]["last_seen"] == LAST.isoformat()


def test_w_seen_covers_wp_and_any_one_sku_is_enough():
    seen = {"GU605C-PQR045W"}  # sku_key form: -WP read as -W
    laptops = [_laptop("L1", ["GU605C-PQR045WP"]), _laptop("L2", ["OLD-1", "GU605C-PQR045W"])]
    assert wp.gone_laptops(laptops, seen, set(), {"rog.asus.com"}) == []


def test_a_laptop_on_a_failed_page_is_left_to_likely_gone():
    assert wp.gone_laptops([_laptop("L1", ["G815L-MS9063W"])], {"X-1"}, {"L1"}, {"rog.asus.com"}) == []


def test_no_sku_or_a_site_not_scraped_is_not_checked():
    laptops = [_laptop("L1", []), _laptop("L2", ["DW3M9PA"], host="www.hp.com"), _laptop("L3", ["T-1"], host=None)]
    assert wp.gone_laptops(laptops, {"X-1"}, set(), {"rog.asus.com"}) == []


def test_the_email_section():
    result = {"steps": {}, "skus_gone": [
        {**_laptop("L1", ["G614P-WRV068W"]), "last_seen": LAST.isoformat()},
        {**_laptop("L2", ["G614P-HRV057W"]), "last_seen": None}]}
    s = {b.split("\n")[0]: b.split("\n")[1:] for b in summary.build_summary(result, 30)[1].split("\n\n")}
    assert s["SKUs no longer on their page"] == [
        "- code-L1 ROG L1 (G614P-WRV068W): last seen 2026-11-15",
        "- code-L2 ROG L2 (G614P-HRV057W): not seen since tracking started"]
