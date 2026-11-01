"""Apple parsers on real saved pages. The processor reads this text, so the spec
test compares against what the old browser scraper returned, frozen."""
import asyncio
import json
from pathlib import Path

import pytest

from ._adapters import apple_scraper

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"
SPECS = "apple_mac_mini_specs.html"
URL = "https://www.apple.com/my/mac-mini/specs/"
PRICES = {"mac-mini-good": "RM\xa03,799", "mac-mini-better": "RM\xa04,699",
          "mac-mini-best": "RM\xa05,599", "mac-mini-m5pro": "RM\xa07,499"}


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _slots() -> dict:
    """Each of this page's real slot names, with the price the browser showed."""
    aliases = apple_scraper._pricing_aliases(_read(SPECS))
    return dict(zip(aliases, PRICES.values()))


def _text(body: str, prices: dict | None = None) -> str:
    html = f'<html><title>Mac</title><div class="techspecs-section">{body}</div></html>'
    return apple_scraper.parse_apple_specs(html, prices or {}, URL)["raw_specs"][0]


def test_spec_text_has_the_same_words_as_the_old_browser_scraper():
    data = apple_scraper.parse_apple_specs(_read(SPECS), _slots(), URL)

    frozen = _read("apple_mac_mini_specs_old_text.txt")
    assert data["raw_specs"][0].split() == frozen.split()
    assert data["status"] == "success"
    assert data["product_name"] == "Mac mini"
    assert data["raw_prices_list"] == []


def test_prices_reach_the_text_from_the_slots():
    text = apple_scraper.parse_apple_specs(_read(SPECS), _slots(), URL)["raw_specs"][0]
    assert "Price\nRM\xa03,799\nRM\xa04,699\nRM\xa05,599\nRM\xa07,499\n" in text


def test_the_price_comes_from_the_page_aliases_and_apples_json(monkeypatch):
    aliases = apple_scraper._pricing_aliases(_read(SPECS))
    assert len(aliases) == 4
    first_slot, first_part = next(iter(aliases.items()))
    asked = []

    def fetch(url):
        asked.append(url)
        return json.dumps({"items": {first_part: {"price": {"display": {"smart": "RM 1"}}}}})

    monkeypatch.setattr(apple_scraper, "_fetch", fetch)

    assert apple_scraper._fetch_prices(_read(SPECS)) == {first_slot: "RM 1"}
    assert asked == [apple_scraper._PRICE_ENDPOINT + ",".join(aliases.values())]


def test_a_page_with_no_aliases_asks_for_no_prices(monkeypatch):
    monkeypatch.setattr(apple_scraper, "_fetch", lambda url: pytest.fail("no aliases, no request"))
    assert apple_scraper._fetch_prices("<html></html>") == {}


def test_a_failed_price_request_fails_the_page_rather_than_dropping_the_prices(monkeypatch):
    # Text with no prices would pass as a success and lose every Apple price
    def fetch(url):
        if "product-price" in url:
            raise RuntimeError("HTTP 503 from price endpoint")
        return _read(SPECS)

    monkeypatch.setattr(apple_scraper, "_fetch", fetch)

    data = asyncio.run(apple_scraper.scrape_official_website(URL, "Apple", "b"))

    assert data["status"] == "failed"
    assert "503" in data["error"]
    assert (data["source_url"], data["brand_id"]) == (URL, "b")


@pytest.mark.parametrize("body, expected", [
    # noscript markup reached the processor as raw tags, 224 lines on a MacBook Pro
    ("<p>Chip</p><noscript><picture><img src='x.png'></picture>raw</noscript>", "Chip"),
    ("<p>Chip</p><script>var a = 1;</script><style>.x{}</style>", "Chip"),
    ("<p>Chip</p><p hidden>secret</p>", "Chip"),
    # hidden by CSS at desktop width
    ('<p>Sky Blue<span class="medium-down">, </span></p>', "Sky Blue"),
    # a fallback for when prices fail
    ('<p>Chip</p><p class="cto-pricing-failed">Configure at apple.com.</p>', "Chip"),
    # read aloud as its own words
    ('<p><span>—</span><span class="visuallyhidden">Not configurable</span></p>', "—\nNot configurable"),
    ("<ul><li>One</li><li>Two</li></ul>", "One\nTwo"),
    ("<p>ProRes encode<br>and decode</p>", "ProRes encode\nand decode"),
    ("<p>  several \n   spaces\t here </p>", "several spaces here"),
    ("<p>Inline <strong>bold</strong> text</p>", "Inline bold text"),
])
def test_text_reads_as_a_browser_shows_it(body, expected):
    assert _text(body) == expected


def test_a_page_with_no_spec_sections_fails_and_says_why():
    # /my/imac/specs/ today: the old scraper failed on it too
    data = apple_scraper.parse_apple_specs("<html><title>iMac</title></html>", {}, URL)
    assert data["status"] == "failed"
    assert "No .techspecs-section" in data["error"]


def test_images_are_absolute_deduped_and_product_shots_only():
    html = """<html><title>Mac</title><div class="techspecs-section"><p>x</p></div>
    <img src="/v/mac/a.jpg"><img src="/v/mac/a.jpg"><img src="https://www.apple.com/v/mac/b.PNG">
    <img src="/v/mac/icon_cpu.png"><img src="/v/logo.png"><img src="/v/meta/x.png"><img src="/v/x_og.png">
    <img src="/v/mac/vector.svg"></html>"""

    images = apple_scraper.parse_apple_specs(html, {}, URL)["image_urls"]

    assert images == ["https://www.apple.com/v/mac/a.jpg", "https://www.apple.com/v/mac/b.PNG"]


def test_listing_gives_the_spec_url_of_every_mac():
    links = apple_scraper.parse_apple_listing(_read("apple_mac_listing.html"))

    assert sorted(links) == [
        "https://www.apple.com/my/imac/specs/",
        "https://www.apple.com/my/mac-mini/specs/",
        "https://www.apple.com/my/mac-studio/specs/",
        "https://www.apple.com/my/macbook-air/specs/",
        "https://www.apple.com/my/macbook-neo/specs/",
        "https://www.apple.com/my/macbook-pro/liquid-retina-xdr-display/specs/",
        "https://www.apple.com/my/macbook-pro/proled-xdr-display/specs/",
    ]


def test_listing_skips_what_is_not_a_mac():
    html = """<a href="/my/mac/accessories/">Learn more</a><a href="/my/studio-display/">Learn more</a>
    <a href="/my/mac/mac-does-that/">Learn more</a><a href="/my/mac-mini/?x=1">Learn more</a>"""
    assert apple_scraper.parse_apple_listing(html) == ["https://www.apple.com/my/mac-mini/specs/"]


def test_an_empty_listing_raises_rather_than_looking_like_a_quiet_day(monkeypatch):
    monkeypatch.setattr(apple_scraper, "_fetch", lambda url: "<html>Access denied</html>")
    with pytest.raises(ValueError, match="No Mac links"):
        asyncio.run(apple_scraper.crawl_apple_specs_links())
