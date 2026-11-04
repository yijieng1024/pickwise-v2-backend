"""10b step (f) plan (app/scripts/discontinue_10b.py). Pure: rows in, changes out."""
from datetime import datetime, timezone

import pytest

from ._adapters import discontinue_10b as d


def _rows(**status):
    """One row per expected id, in its expected state; *status* overrides by prefix."""
    return [{"id": f"{k}-0000-0000-0000-000000000000", "product_name": f"laptop {k}",
             "status": status.get(k, v), "price_rm": 0.0, "price_checked_at": None, "history": []}
            for k, v in d.EXPECTED.items()]


def test_every_row_goes_to_discontinued_and_only_status_is_named():
    result = d.plan(_rows())
    assert len(result) == 12
    assert {(e["before"], e["after"]) for e in result} == {("active", "discontinued"), ("suspended", "discontinued")}


def test_a_wrong_current_status_refuses_the_whole_plan():
    with pytest.raises(d.Refused, match="0670a4d3: status is 'inactive'"):
        d.plan(_rows(**{"0670a4d3": "inactive"}))


def test_a_missing_id_refuses():
    rows = [r for r in _rows() if not r["id"].startswith("7d7e74c8")]
    with pytest.raises(d.Refused, match="7d7e74c8: 0 rows match"):
        d.plan(rows)


def test_a_prefix_matching_two_rows_refuses():
    rows = _rows() + [{**_rows()[0], "id": "0670a4d3-ffff-0000-0000-000000000000"}]
    with pytest.raises(d.Refused, match="0670a4d3: 2 rows match"):
        d.plan(rows)


def test_last_official_price_is_shown():
    rows = _rows()
    at = datetime(2026, 6, 25, tzinfo=timezone.utc)
    rows[0]["history"] = [(8299.0, at, None)]
    result = {e["id"][:8]: e["last_official"] for e in d.plan(rows)}
    assert result["0670a4d3"] == (8299.0, at)
    assert result["128dcb6b"] == (None, None)
