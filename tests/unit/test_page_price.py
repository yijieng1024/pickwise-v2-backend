"""The price a raw row holds, read for the "Last checked" stamp of an unchanged page."""
import pytest

from ._adapters import bulk_scraper


@pytest.mark.parametrize("raw, price", [
    ([{"price": "RM12,999.00"}], 12999.0),
    ([{"price": "N/A"}], 0.0),
    ([{"price": "RM0.00"}], 0.0),
    (["RM4,999"], 4999.0),
    ([], 0.0),
    (None, 0.0),
])
def test_the_price_a_raw_row_holds(raw, price):
    assert bulk_scraper._page_price(raw) == price
