from app.processor.engine import _filter_variant_images


def test_keeps_matching_size_and_drops_other_sizes():
    urls = ["a/13-inch-air.png", "a/15-inch-air.png", "a/hero.png"]
    assert _filter_variant_images(urls, 13.6) == ["a/13-inch-air.png", "a/hero.png"]


def test_drops_social_preview_cards():
    assert _filter_variant_images(["x/meta/y_og.png", "x/hero.png"], None) == ["x/hero.png"]


def test_falls_back_when_size_filter_drops_everything():
    # HP ships the 14" photo file on the 16" OmniBook 5.
    urls = ["hp-omnibook-5-14-inch-laptop.png"]
    assert _filter_variant_images(urls, 16.0) == urls
