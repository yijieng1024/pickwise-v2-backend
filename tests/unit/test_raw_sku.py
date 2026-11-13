"""The SKU a scraper leaves in a raw row."""
import pytest

from ._adapters import processor_engine

raw_sku = processor_engine.raw_sku


@pytest.mark.parametrize("dump, expected", [
    ({"scraped_features": {"SKU": " G835L-WGSA478WP "}}, "G835L-WGSA478WP"),
    ({"scraped_features": {"SKU": ""}}, None),
    ({"scraped_features": {"Price": "N/A"}}, None),
    ({"scraped_features": ["a page with a list of specs"]}, None),  # Apple
    # HP: the part number, in the page's own spec table
    ({"scraped_features": {"Product Info / Sku": " DP7X3PA ", "Price": "RM4,999.00"}}, "DP7X3PA"),
    ({"scraped_features": {"Product Info / Sku": ""}}, None),
    ({"scraped_features": {"SKU": "G835L-WGSA478WP", "Product Info / Sku": "DP7X3PA"}}, "G835L-WGSA478WP"),
    ({}, None),
    (None, None),
])
def test_raw_sku(dump, expected):
    assert raw_sku(dump) == expected
