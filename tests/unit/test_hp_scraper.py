"""HP parsers on real saved pages, cut down to what they read."""
import asyncio
from pathlib import Path

import pytest

from ._adapters import hp_scraper

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"
PRODUCT = "hp_product_d09rrat.html"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_product_page_gives_specs_price_part_number_and_name():
    product = hp_scraper.parse_hp_product(_read(PRODUCT))

    assert product["status"] == "success"
    assert product["source_url_suffix"] == ""
    assert product["product_name"] == "HP ZBook 8 G1ah 14 inch Mobile Workstation PC Silver"
    assert product["specs"]["Price"] == "RM10,014.00"
    assert product["specs"]["Product Info / Sku"] == "D09RRAT"
    assert len(product["specs"]) == 47  # 46 rows, section-prefixed, plus Price
    assert product["specs"]["Product Info / Title"].endswith("(D09RRAT)")


def test_only_the_gallery_photos_are_kept():
    # Hand-added: this page's raw HTML has no such image, a browser adds them.
    accessory = "https://my-media.apjonlinecdn.com/catalog/product/cache/" + "0" * 32 + "/a/c/accessory.png"
    html = _read(PRODUCT).replace("</body>", f'<img src="{accessory}"></body>')

    product = hp_scraper.parse_hp_product(html)

    assert len(product["image_urls"]) == 11
    assert all("/catalog/product/" in url for url in product["image_urls"])
    assert accessory not in product["image_urls"]


def test_the_final_price_is_the_fallback_not_the_first_price_node():
    # Without mainSKU, the price comes from the page. Magento pages carry an
    # untyped instalment figure and an RRP as well.
    html = _read(PRODUCT).replace(hp_scraper._MAIN_SKU, "pagePropertiesData.other = {")
    instalment = '<span data-price-amount="835" data-price-type=""></span>'
    html = html.replace('<div class="product-info-main">', f'<div class="product-info-main">{instalment}')

    product = hp_scraper.parse_hp_product(html)

    assert product["specs"]["Price"] == "RM10,014.00"
    assert product["product_name"] == "HP ZBook 8 G1ah 14 inch Mobile Workstation PC, Silver"


def test_a_page_with_no_price_says_na():
    html = _read(PRODUCT).replace(hp_scraper._MAIN_SKU, "pagePropertiesData.other = {")
    html = html.replace("data-price-amount", "data-gone")
    assert hp_scraper.parse_hp_product(html)["specs"]["Price"] == "N/A"


def test_a_page_with_no_spec_rows_raises():
    with pytest.raises(ValueError, match="No spec rows"):
        hp_scraper.parse_hp_product("<html><body>Access denied</body></html>")


def test_main_sku_is_read_never_executed():
    html = _read(PRODUCT).replace(hp_scraper._MAIN_SKU, hp_scraper._MAIN_SKU + " name: stealMyCookies(),")
    with pytest.raises(ValueError):
        hp_scraper.parse_hp_product(html)


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def test_listing_gives_product_links_only():
    links = hp_scraper.parse_hp_listing(_read("hp_listing_p1.html"))

    assert len(links) == 15
    assert all(link.startswith("https://www.hp.com/my-en/shop/") and link.endswith(".html") for link in links)
    assert not any("not-a-product" in link for link in links)


def test_a_query_string_on_a_link_is_dropped():
    html = '<a class="product-item-link" href="https://www.hp.com/my-en/shop/x.html?ref=1">x</a>'
    assert hp_scraper.parse_hp_listing(html) == ["https://www.hp.com/my-en/shop/x.html"]


def _listing(*links: str) -> str:
    return "".join(f'<a class="product-item-link" href="https://h/{link}.html">x</a>' for link in links)


def test_pagination_stops_on_a_page_with_no_new_links():
    pages = {1: _listing("a", "b"), 2: _listing("b", "c"), 3: _listing("a", "c")}
    asked = []

    def fetch(page):
        asked.append(page)
        return pages[page]

    assert hp_scraper.crawl_hp_listing(fetch) == ["https://h/a.html", "https://h/b.html", "https://h/c.html"]
    assert asked == [1, 2, 3]


def test_an_empty_first_page_raises_rather_than_looking_like_a_quiet_day():
    with pytest.raises(ValueError, match="page 1 has no product links"):
        hp_scraper.crawl_hp_listing(lambda page: "<html>Access denied</html>")


def test_a_failed_page_is_a_failed_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(hp_scraper, "_fetch", lambda url: "<html>Access denied</html>")
    results = asyncio.run(hp_scraper.scrape_hp_laptop_specs("https://h/p.html", None))
    assert [r["status"] for r in results] == ["failed"]


def test_a_scrape_keeps_the_raw_row_shape(monkeypatch):
    monkeypatch.setattr(hp_scraper, "_fetch", lambda url: _read(PRODUCT))
    [result] = asyncio.run(hp_scraper.scrape_hp_laptop_specs("https://h/p.html", None))
    assert result["raw_prices_list"] == [{"price": "RM10,014.00"}]
    assert result["raw_specs"]["Product Info / Sku"] == "D09RRAT"
    assert result["source_url_suffix"] == ""
