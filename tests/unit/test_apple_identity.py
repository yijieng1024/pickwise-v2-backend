"""
Apple identity helpers (app/processor/apple_name.py). The upsert that uses them is
tested in tests/integration/test_apple_identity.py.
"""
import pytest

from tests.unit._adapters import apple_name

UnknownLine = apple_name.UnknownLine
build_name = apple_name.build_name
identity = apple_name.identity
product_and_line = apple_name.product_and_line
with_line_token = apple_name.with_line_token

PROLED = "https://www.apple.com/my/macbook-pro/proled-xdr-display/specs/"
LIQUID = "https://www.apple.com/my/macbook-pro/liquid-retina-xdr-display/specs/"
OLD_PRO = "https://www.apple.com/my/macbook-pro/specs/"
AIR = "https://www.apple.com/my/macbook-air/specs/"
SLUG = "apple-macbook-pro-14-m5-pro-15c-16g-24gb-2048gb"


def test_apple_code_has_line():
    """
    Catches the 2026-11-03 overwrite: the PROLED and Liquid Retina pages gave the same
    slug, so the PROLED page updated a June Liquid Retina row in place. The line comes
    from the URL, and the old single page is the Liquid Retina machine.
    """
    proled = with_line_token(SLUG, product_and_line(PROLED)[1])
    liquid = with_line_token(SLUG, product_and_line(LIQUID)[1])
    assert proled == "apple-macbook-pro-14-proled-m5-pro-15c-16g-24gb-2048gb"
    assert liquid == "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-2048gb"
    assert with_line_token(SLUG, product_and_line(OLD_PRO)[1]) == liquid
    # Idempotent, so re-processing a row never stacks tokens
    assert with_line_token(proled, "proled") == proled
    # Air and Neo are one line each: no token
    assert product_and_line(AIR) == ("MacBook Air", None)
    assert with_line_token("apple-macbook-air-13-m5-10c-10g-16gb-512gb", None) == (
        "apple-macbook-air-13-m5-10c-10g-16gb-512gb"
    )


@pytest.mark.parametrize("url", [
    "https://www.apple.com/my/macbook-pro/nano-texture/specs/",
    "https://www.apple.com/my/macbook-pro/",
    "https://www.apple.com/my/mac-mini/specs/",
])
def test_unknown_macbook_pro_url_has_no_line(url):
    """An unknown page must never default to liquid-retina."""
    with pytest.raises(UnknownLine):
        product_and_line(url)


def test_apple_name_is_built_from_specs():
    """Both lines are marked, so no two configurations share a name."""
    assert build_name("MacBook Pro", "liquid-retina", 14.2, "Apple M5 Pro chip", 15, 16, 24, 2048) == (
        "14-inch MacBook Pro Liquid Retina XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 2TB SSD)"
    )
    assert build_name("MacBook Pro", "proled", 14.2, "Apple M5 Pro (15-core)", 15, 16, 24, 2048) == (
        "14-inch MacBook Pro PROLED XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 2TB SSD)"
    )
    assert build_name("MacBook Air", None, 13.6, "Apple M5 chip", 10, 8, 16, 512) == (
        "13-inch MacBook Air (M5, 10-core CPU, 8-core GPU, 16GB RAM, 512GB SSD)"
    )


def test_identity_compares_size_chip_and_line():
    """Wording does not count ("(15-core)" vs "chip"); size, chip and line do."""
    code = with_line_token(SLUG, "liquid-retina")
    june = identity(14.2, "Apple M5 Pro (15-core)", code)
    assert june == identity(14.2, "Apple M5 Pro chip", code)
    assert june != identity(16.2, "Apple M5 Pro chip", code)
    assert june != identity(14.2, "Apple M5 Max chip", code)
    assert june != identity(14.2, "Apple M5 Pro chip", with_line_token(SLUG, "proled"))
