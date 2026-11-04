"""last_official_price (laptop_models): the discontinued details page's "Last official
price", and lookup_laptop's (ADR-0009 Amendment II, decided 2026-11-04)."""
from datetime import datetime, timezone

from ._adapters import laptop_models as lm
from ._adapters import lookup_laptop_tool as lt


def _at(day):
    return datetime(2026, 6, day, tzinfo=timezone.utc)


def test_the_newest_official_row_with_its_date():
    history = [(6999.0, _at(22), None), (8299.0, _at(25), None)]
    assert lm.last_official_price(8299.0, None, history) == (8299.0, _at(25))


def test_rm_0_and_no_history_is_no_price():
    assert lm.last_official_price(0.0, _at(25), []) == (None, None)


def test_rm_0_now_still_shows_the_last_real_price():
    assert lm.last_official_price(0.0, None, [(5999.0, _at(22), "manual")]) == (5999.0, _at(22))


def test_a_source_outside_the_official_list_is_not_used():
    history = [(8299.0, _at(22), "guide"), (7100.0, _at(28), "reseller-feed")]
    assert lm.last_official_price(7100.0, _at(29), history) == (8299.0, _at(22))


def test_a_newer_check_dates_the_current_price():
    history = [(8299.0, _at(22), "apple-buy-page")]
    assert lm.last_official_price(8299.0, _at(29), history) == (8299.0, _at(29))


def test_lookup_says_no_recorded_price_never_rm_0():
    row = {"id": 1, "product_name": "x", "status": "discontinued", "price_rm": 0.0,
           "last_price_rm": None, "last_price_date": None}
    out = lt._config(row)
    assert out["last_official_price"] == "No recorded price"
    assert "last_official_price_rm" not in out


def test_an_active_laptop_carries_its_price_not_a_last_price():
    out = lt._config({"id": 1, "product_name": "x", "status": "active", "price_rm": 4999.0,
                      "last_price_rm": 1.0, "last_price_date": "2026-06-22"})
    assert out["price_rm"] == 4999.0 and not any(k.startswith("last_official") for k in out)


def test_a_zero_row_in_history_is_not_a_price():
    history = [(8299.0, _at(22), None), (0.0, _at(28), "page")]
    assert lm.last_official_price(0.0, None, history) == (8299.0, _at(22))


def test_naive_history_dates_compare_with_an_aware_check():
    """recorded_at comes back naive, price_checked_at aware: production crashed on this."""
    naive = datetime(2026, 6, 25)
    assert lm.last_official_price(8299.0, _at(29), [(8299.0, naive, None)]) == (8299.0, _at(29))
