"""search_malaysian_market_price on a discontinued laptop: the last official price, and no
store search at all, so nothing beside it reads as on sale. The status filter itself is
SQL: tests/integration/test_market_price_status.py."""
from contextlib import nullcontext

import pytest

from ._adapters import market_price

DISCONTINUED = {"found": True, "product_name": "ROG Strix G16 (2025) G614 (G614P-RRV012W)", "status": "discontinued",
                "no_longer_sold": True, "last_official_price_rm": 9999.0, "last_official_price_date": "2026-08-15"}
ACTIVE = {"found": True, "product_name": "ROG Strix G16 (2025) G615", "official_price_rm": 8799.0,
          "price_history": []}


@pytest.fixture
def searched(monkeypatch):
    calls = []
    monkeypatch.setattr(market_price, "Session", lambda _engine: nullcontext())
    monkeypatch.setattr(market_price, "_live_listings", lambda q: calls.append(q) or {"status": "ok", "listings": [1]})
    return calls


def _run(monkeypatch, catalog):
    monkeypatch.setattr(market_price, "_catalog_lookup", lambda q, code, s: catalog)
    return market_price.search_malaysian_market_price.func("ROG Strix G16")


def test_a_discontinued_laptop_is_not_searched_in_any_store(monkeypatch, searched):
    out = _run(monkeypatch, DISCONTINUED)
    assert searched == [], "a store search beside a discontinued laptop reads as still on sale"
    assert out["live_listings"] == {"status": "not_searched", "listings": [],
                                    "note": "Discontinued: no longer sold, so stores were not searched."}
    assert not any("shopee" in k or "lazada" in k for k in out), "marketplace links make it look on sale"
    assert out["catalog"] is DISCONTINUED


def test_an_active_laptop_is_searched_as_before(monkeypatch, searched):
    out = _run(monkeypatch, ACTIVE)
    assert searched == ["ROG Strix G16"]
    assert out["live_listings"]["status"] == "ok"
    assert "shopee_search_url" in out and "lazada_search_url" in out


def test_a_laptop_not_in_the_catalog_is_still_searched(monkeypatch, searched):
    out = _run(monkeypatch, {"found": False})
    assert searched == ["ROG Strix G16"] and "shopee_search_url" in out


def test_the_eval_fails_a_discontinued_answer_that_names_a_marketplace():
    from ._adapters import run_eval

    def check(text):
        result = {"text": text, "tool_calls": [], "tool_outputs": []}
        return [c for c in run_eval.rule_checks({"no_marketplace": True}, "tool_routing", result, [], [])
                if c["name"] == "no_marketplace_listings"]

    [bad] = check("It's discontinued, but Shopee has it for RM9,499.")
    assert bad["passed"] is False and "shopee" in bad["detail"]
    [good] = check("It's discontinued. Its last official price was RM10,699 on 14 Aug 2026.")
    assert good["passed"] is True
    result = {"text": "Shopee", "tool_calls": [], "tool_outputs": []}
    assert "no_marketplace_listings" not in [c["name"] for c in run_eval.rule_checks({}, "tool_routing", result, [], [])]
