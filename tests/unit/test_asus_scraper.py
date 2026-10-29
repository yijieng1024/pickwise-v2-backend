"""ASUS www techspec parser and listing discovery, on saved real pages."""
from pathlib import Path

import pytest

from ._adapters import asus_scraper, nuxt_state

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# www.asus.com /techspec/ (Nuxt 3)
# --------------------------------------------------------------------------


def test_techspec_page_gives_every_variant():
    variants = asus_scraper.parse_asus_techspec(_read("asus_techspec_m1607.html"))

    assert [v["product_name"] for v in variants] == [
        "ASUS Vivobook 16 (M1607); Copilot+ PC (M1607KA)",
        "ASUS Vivobook 16 (M1607); Copilot+ PC (M1607GA)",
    ]
    assert [v["source_url_suffix"] for v in variants] == ["?v=1", "?v=2"]
    assert all(v["status"] == "success" for v in variants)
    assert "AMD Ryzen™ AI 7 350" in variants[0]["specs"]["Processor"]
    assert "AMD Ryzen™ AI 7 445" in variants[1]["specs"]["Processor"]
    assert len(variants[0]["specs"]) == 29  # 28 page specs + Price
    assert variants[0]["image_urls"] == [
        "https://dlcdnwebimgs.asus.com/gain/4118dfdd-2668-4385-8783-4de29a2d4935/"
    ]


@pytest.mark.parametrize("html", [
    # the pre-2026 Nuxt 2 page shape
    "<script>window.__NUXT__=(function(a){return {state:{PDPage:{}}}}(1))</script>",
    # a block / challenge page
    "<html><title>Just a moment...</title><body>Checking your browser</body></html>",
])
def test_page_without_nuxt3_payload_raises(html):
    with pytest.raises(nuxt_state.NuxtStateError, match="no __NUXT_DATA__"):
        asus_scraper.parse_asus_techspec(html)


def test_www_page_price_is_never_used():
    # The fixture's PDPriceList carries RM3999 for the first variant
    variants = asus_scraper.parse_asus_techspec(_read("asus_techspec_m1607_priced.html"))
    assert [v["specs"]["Price"] for v in variants] == ["N/A", "N/A"]


# --------------------------------------------------------------------------
# Discovery: SeriesFilterResult listing API
# --------------------------------------------------------------------------


def _two_pages(page: int) -> str:
    # a full page (20) then the short last page (10); a third fetch is a bug
    return {1: _read("asus_sfr_p3.json"), 2: _read("asus_sfr_p7.json")}[page]


def test_discovery_pages_until_short_page_and_drops_chromebooks():
    urls = asus_scraper.crawl_series(_two_pages)

    # 30 entries on the two pages, 2 of them Chromebooks
    assert len(urls) == 28
    assert not [u for u in urls if "chromebook" in u.lower()]
    assert sum("rog.asus.com" in u for u in urls) == 3
    assert all(u.endswith("/") and "?" not in u for u in urls)


def test_empty_first_page_raises():
    empty = '{"Status": "0", "Message": "", "Result": {"TotalCount": 0, "ProductList": []}}'
    with pytest.raises(ValueError, match="page 1 has no products"):
        asus_scraper.crawl_series(lambda page: empty)


def test_rog_cards_for_the_same_page_give_one_url():
    rog = "https://rog.asus.com/my/laptops/rog-strix/rog-strix-g16-2026/"
    products = [{"Name": "G16 a", "ProductURL": rog}, {"Name": "G16 b", "ProductURL": rog}]
    assert asus_scraper.laptop_urls(products) == [rog]


@pytest.mark.parametrize("raw, expected", [
    ("/my/laptops/for-home/vivobook/x/", "https://www.asus.com/my/laptops/for-home/vivobook/x/"),
    ("https://www.asus.com/my/laptops/a/b/techspec/", "https://www.asus.com/my/laptops/a/b/"),
    ("https://rog.asus.com/my/laptops/rog-strix/g16/spec", "https://rog.asus.com/my/laptops/rog-strix/g16/"),
    ("https://www.asus.com/my/laptops/a/b?utm=x#top", "https://www.asus.com/my/laptops/a/b/"),
])
def test_product_url_normalisation(raw, expected):
    assert asus_scraper._normalise_product_url(raw) == expected


# --------------------------------------------------------------------------
# Series-only pages (asus-tuf-gaming-16-2026, asus-expertbook-b5-g2-16-intel):
# PDTechSpec.SpecList is a list of {Title, Content}, so the parser raises, and
# a bulk run records the page as one failed target (Jack, 2026-10-29: kept as
# a visible failure)
# --------------------------------------------------------------------------

# devalue payload: pinia.pdPageStore.PDTechSpec.SpecList = [{Title, Content}]
_SERIES_ONLY_PAGE = (
    '<script type="application/json" id="__NUXT_DATA__">'
    '[{"pinia":1},{"pdPageStore":2},{"PDTechSpec":3},{"SpecList":4},[5],'
    '{"Title":6,"Content":7},"Processor","Intel Core Ultra 5"]'
    "</script>"
)


def test_series_only_page_is_a_failed_target_in_a_bulk_run(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from ._adapters import bulk_scraper

    monkeypatch.setattr(asus_scraper, "_fetch", lambda url: _SERIES_ONLY_PAGE)
    monkeypatch.setattr(bulk_scraper, "LIVE_FETCH_PAUSE_S", 0)
    monkeypatch.setattr(bulk_scraper, "_update_target", lambda s, t, status: setattr(t, "scrape_status", status))
    session = SimpleNamespace(
        exec=lambda stmt: SimpleNamespace(first=lambda: None),  # not scraped before
        commit=lambda: None,
        add=lambda row: pytest.fail("a failed page must not write a raw row"),
    )
    target = SimpleNamespace(url="https://www.asus.com/my/laptops/for-gaming/tuf-gaming/asus-tuf-gaming-16-2026/")
    brand = SimpleNamespace(name="ASUS", id=None)
    report = bulk_scraper.BulkScrapeReport(brand_name="ASUS", total_pending=1)

    asyncio.run(bulk_scraper._process_target(session, target, brand, report))

    assert (report.processed, report.failed, report.succeeded) == (1, 1, 0)
    assert report.results[0].status == "failed"
    assert "SpecList is a list" in report.results[0].error
    assert target.scrape_status == bulk_scraper.ScrapeStatus.FAILED
