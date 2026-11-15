"""The guide "reviewed" mark (Jack, 2026-11-15, option 2b): a reviewed row leaves the
waiting and differs lists until the guide price for that laptop and SKU changes, carried
across guide editions; -W and -WP are one SKU."""
import uuid

from tests.unit._adapters import price_guide

L1, L2 = uuid.uuid4(), uuid.uuid4()


def test_same_laptop_sku_and_price_is_reviewed():
    reviewed = {price_guide.review_key(L1, "GU605C-PQR045W", 14999.0)}
    assert price_guide.is_reviewed(reviewed, L1, "GU605C-PQR045W", 14999.0)


def test_wp_and_w_are_one_sku():
    reviewed = {price_guide.review_key(L1, "GU605C-PQR045W", 14999.0)}
    assert price_guide.is_reviewed(reviewed, L1, "GU605C-PQR045WP", 14999.0)


def test_a_new_guide_price_brings_it_back():
    reviewed = {price_guide.review_key(L1, "GU605C-PQR045W", 14999.0)}
    assert not price_guide.is_reviewed(reviewed, L1, "GU605C-PQR045W", 13999.0)


def test_another_laptop_or_sku_is_not_reviewed():
    reviewed = {price_guide.review_key(L1, "BM1403CDA-S61806X", 5799.0)}
    assert not price_guide.is_reviewed(reviewed, L2, "BM1403CDA-S61806X", 5799.0)
    assert not price_guide.is_reviewed(reviewed, L1, "BM1403CDA-S61807X", 5799.0)
