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


def test_apple_processor_and_brightness_built_by_code():
    """The round 3 repair's values are what the processor now writes, so re-running a page
    cannot undo them: the June processor form, and peak SDR by line."""
    assert apple_name.processor_name("Apple M5 Pro chip", 15) == "Apple M5 Pro (15-core)"
    assert apple_name.processor_name("Apple M5 Pro chip (15-core)", 15) == "Apple M5 Pro (15-core)"
    assert apple_name.processor_name("Apple M6 chip", 12) == "Apple M6 (12-core)"
    assert apple_name.brightness("proled", 1600) == 2000
    assert apple_name.brightness("liquid-retina", 1600) == 1000
    assert apple_name.brightness(None, 500) == 500


def _size(inch):
    return (f"\nFinish\nSilver\n\nChip\nApple M5 Pro chip\n\nSize and Weight\nWidth: {inch}cm\n"
            f"\nOperating System\nmacOS app list\n\nIn the Box\n{inch}-inch MacBook Pro\n\nEnvironment\ncopy\n")


def test_size_sections_split_per_size():
    """One section per size, cut before the app list, with its own In the Box paragraph."""
    sections = apple_name.size_sections("Product image" + _size(14) + _size(16), "MacBook Pro")
    assert len(sections) == 2
    assert "Width: 14cm" in sections[0] and "14-inch MacBook Pro" in sections[0]
    assert "Width: 16cm" in sections[1] and "14cm" not in sections[1]
    assert not any("macOS app list" in s or "Environment" in s for s in sections)
    # Neo is one size: whole, unchanged
    assert apple_name.size_sections("anything", "MacBook Neo") == ["anything"]


@pytest.mark.parametrize("text", [
    "Product image" + _size(14),                              # one size where two are expected
    "Product image" + _size(14) + _size(16) + _size(18),      # three
    "Product image" + _size(14) + _size(16).replace("Size and Weight", "Dimensions"),
])
def test_size_sections_refuse_an_unexpected_layout(text):
    with pytest.raises(apple_name.SplitFailed):
        apple_name.size_sections(text, "MacBook Pro")


def test_size_sections_refuse_an_oversized_section():
    long = "Product image" + _size(14).replace("Chip\n", "Chip\n" + "x" * 13_000) + _size(16)
    with pytest.raises(apple_name.SplitFailed, match="over"):
        apple_name.size_sections(long, "MacBook Pro")


def test_processor_name_has_no_fallback():
    """No core count, no name: never the LLM's own wording."""
    assert apple_name.processor_name("Apple M5 Pro chip", None) is None
    assert apple_name.processor_name("Intel Core i7", 8) is None
