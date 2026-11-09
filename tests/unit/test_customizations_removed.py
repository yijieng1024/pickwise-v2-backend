"""10c (g): the old /customizations router is gone; Apple options live in laptop_options."""
from tests.unit._adapters import registered_paths


def test_no_customization_routes_are_registered():
    paths = registered_paths()
    assert any(p.startswith("/api/v2/laptops") for p in paths), "sanity: the app's routes were read"
    assert not [p for p in paths if "customization" in p]
