"""The round 3 Apple repair decides everything in plan(); these check it."""
from datetime import datetime

import pytest

from tests.unit._adapters import repair_apple_round3 as repair

C07, F5, B857, A431 = repair.CLEAR_PRICE, repair.UNSUSPEND, *(i for i, _ in repair.NEW_ROWS)
DONOR = {"materials_and_certifications": "recycled", "bundled_accessories": "cable", "warranty_details": "1 year"}
CODE = "apple-macbook-pro-14-{}-m5-pro-15c-16g-{}"


def _row(id_, line, ram, ssd, **kw):
    row = dict(
        id=id_, model_code=CODE.format(line, f"{ram}gb-{ssd}gb"), product_name="old", status="inactive",
        processor_model="Apple M5 Pro chip", cpu_cores=15, gpu_cores=16, ram_gb=ram, ssd_gb=ssd,
        display_size_inch=14.2, display_brightness_nits=1600, price_rm=0.0, family_id="fam",
        created_at=datetime(2026, 11, 3, 6, 53), updated_at=None, price_checked_at=None, manufacturer_sku=None,
        materials_and_certifications=None, bundled_accessories=None, warranty_details=None,
    )
    row.update(kw)
    return row


def _catalog():
    return [
        _row(C07, "liquid-retina", 24, 2048, price_rm=12549.0, display_brightness_nits=2000),
        _row(F5, "proled", 48, 1024, status="suspended"),
        _row(B857, "proled", 48, 2048, display_brightness_nits=2000),
        _row(A431, "liquid-retina", 24, 1024, price_rm=10299.0),
        _row("june", "liquid-retina", 24, 512, status="active", processor_model="Apple M5 Pro (15-core)",
             display_brightness_nits=1000, product_name="14-inch MacBook Pro Liquid Retina XDR (M5 Pro, 15-core CPU, "
             "16-core GPU, 24GB RAM, 512GB SSD)", **DONOR),
    ]


HISTORY = [
    {"id": "h1", "laptop_id": C07, "price_rm": 10699.0, "recorded_at": datetime(2026, 6, 22)},
    {"id": "h2", "laptop_id": C07, "price_rm": 9999.0, "recorded_at": "2026-11-04T00:00:00+00:00"},
]


def _applied(rows, result):
    by_id = {r["id"]: dict(r) for r in rows}
    for row, diff in result["updates"]:
        by_id[row["id"]].update({k: new for k, (_, new) in diff.items()})
    for n, (_, fields) in enumerate(result["inserts"]):
        by_id[f"new{n}"] = {"id": f"new{n}", **fields}
    return list(by_id.values())


def test_repair_plan():
    """Each row becomes the machine its data describes; the two missing configurations
    get rows; only the June price history goes; the active row is left alone."""
    result = repair.plan(_catalog(), DONOR, HISTORY)
    diff = {row["id"]: d for row, d in result["updates"]}

    assert diff[C07]["model_code"][1] == CODE.format("proled", "24gb-2048gb")
    assert diff[C07]["price_rm"][1] == 0.0
    assert "materials_and_certifications" not in diff[C07], "a PROLED row was filled from a Liquid Retina one"
    assert diff[F5]["model_code"][1] == CODE.format("liquid-retina", "48gb-1024gb")
    assert diff[F5]["status"] == ("suspended", "inactive")
    assert diff[F5]["display_brightness_nits"] == (1600, 1000)
    assert diff[F5]["warranty_details"] == (None, "1 year")
    assert diff[A431]["processor_model"][1] == "Apple M5 Pro (15-core)"
    assert "price_rm" not in diff[A431] and "status" not in diff[B857]
    assert "june" not in diff

    assert [(f["model_code"], f["price_rm"], f["status"]) for _, f in result["inserts"]] == [
        (CODE.format("proled", "48gb-1024gb"), 0.0, "inactive"),
        (CODE.format("liquid-retina", "24gb-2048gb"), 0.0, "inactive"),
    ]
    assert [h["id"] for h in result["delete_history"]] == ["h1"]
    assert {i for i, _ in result["empty_proled"]} == {C07, B857}


def test_second_run_does_nothing():
    first = repair.plan(_catalog(), DONOR, HISTORY)
    left = [h for h in HISTORY if h not in first["delete_history"]]
    again = repair.plan(_applied(_catalog(), first), DONOR, left)
    assert (again["updates"], again["inserts"], again["delete_history"]) == ([], [], [])


def test_collision_refused():
    """The unique index: another row already holding a planned code stops the plan."""
    rows = _catalog() + [_row("other", "liquid-retina", 48, 1024, product_name="x")]
    with pytest.raises(repair.Refused, match="collision"):
        repair.plan(rows, DONOR, HISTORY)


def test_active_row_change_refused():
    """An active row may change brightness and processor_model only."""
    rows = _catalog()
    rows[-1]["warranty_details"] = None
    with pytest.raises(repair.Refused, match="active row"):
        repair.plan(rows, DONOR, HISTORY)
