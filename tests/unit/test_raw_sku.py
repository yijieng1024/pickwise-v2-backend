"""The SKU a scraper leaves in a raw row."""
import pytest

from app.processor.engine import raw_sku


@pytest.mark.parametrize("dump, expected", [
    ({"scraped_features": {"SKU": " G835L-WGSA478WP "}}, "G835L-WGSA478WP"),
    ({"scraped_features": {"SKU": ""}}, None),
    ({"scraped_features": {"Price": "N/A"}}, None),
    ({"scraped_features": ["a page with a list of specs"]}, None),  # HP and Apple
    ({}, None),
    (None, None),
])
def test_raw_sku(dump, expected):
    assert raw_sku(dump) == expected
