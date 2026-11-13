"""What the first ROG and HP re-scrape would do. `plan` decides everything, so no database
and no fetch."""
from types import SimpleNamespace

from tests.unit._adapters import rescrape_plan as plan

HP = "hp-brand"


def _laptop(name, sku=None, laptop_id="a", brand=HP, status="active"):
    return SimpleNamespace(id=laptop_id, brand_id=brand, product_name=name, model_code=f"slug-{laptop_id}",
                           manufacturer_sku=sku, status=status)


def _variant(name, sku, stored="changed", brand=HP, url="https://www.hp.com/p.html"):
    return SimpleNamespace(source_url=url, brand_id=brand, name=name, sku=sku, stored=stored)


def _outcomes(variants, laptops):
    return [(row["outcome"], row["laptop"].id if row["laptop"] else None) for row in plan(variants, laptops)]


def test_an_unchanged_page_is_not_processed_whatever_its_sku():
    assert _outcomes([_variant("HP 15", "D93CDPA", stored="unchanged")], [_laptop("HP 15", sku="D93CDPA")]) == [
        ("unchanged", None)]


def test_a_stored_page_fetched_with_no_sku_keeps_its_row():
    assert _outcomes([_variant("HP 15", None)], [_laptop("HP 15")]) == [("no SKU", None)]


def test_a_laptop_holding_the_sku_is_updated_and_w_reads_as_wp():
    rog = _laptop("ROG Strix G16 (G614P-MTS088WP)", sku="G614P-MTS088WP", brand="asus")
    assert _outcomes([_variant("Any other name", "G614P-MTS088W", brand="asus")], [rog]) == [("SKU", "a")]


def test_no_holder_and_a_close_name_is_a_slug_match():
    laptop = _laptop("HP OmniBook X 14 inch Laptop 14-ka0071TU (Intel Core Ultra 5 325, 16GB RAM)")
    assert _outcomes([_variant("HP OmniBook X 14 inch Laptop 14-ka0071TU Blue", "DP7X3PA")], [laptop]) == [
        ("slug", "a")]


def test_a_second_sku_on_the_same_laptop_is_an_insert():
    # Two pages of one machine (two colours): the first takes the laptop and
    # leaves its SKU on it, so the processor gives the second its own row.
    laptop = _laptop("HP ProBook 4 G1i 14 inch Notebook AI PC (Intel Core Ultra 5 225U)")
    rows = plan([_variant("HP ProBook 4 G1i 14 inch Notebook AI PC Silver", "DJ0L0AT"),
                 _variant("HP ProBook 4 G1i 14 inch Notebook AI PC Blue", "DJ0L1AT")], [laptop])
    assert sorted(r["outcome"] for r in rows) == ["insert", "slug"]
    assert [r["note"][:20] for r in rows if r["outcome"] == "insert"] == ["closest laptop holds"]
    assert laptop.manufacturer_sku is None, "plan changed the laptops it was given"


def test_no_laptop_close_by_name_is_an_insert_with_the_closest_listed():
    rows = plan([_variant("HP ZBook Fury 18 G2 Mobile Workstation", "ZZ1Z1PA")], [_laptop("HP 15.6 inch Laptop")])
    assert (rows[0]["outcome"], rows[0]["laptop"].id, rows[0]["note"]) == ("insert", "a", "no laptop this close by name")


def test_only_laptops_of_the_same_brand_are_slug_candidates():
    assert _outcomes([_variant("HP 15.6 inch Laptop", "D93CDPA")], [_laptop("HP 15.6 inch Laptop", brand="asus")]) == [
        ("insert", None)]


def test_a_page_named_exactly_as_a_laptop_takes_it_before_a_similar_page():
    laptop = _laptop("HP 14 inch Laptop 14-em0299AU Silver")
    rows = plan([_variant("HP 14 inch Laptop 14-em0298AU Silver", "E15ZYPA"),
                 _variant("HP 14 inch Laptop 14-em0299AU Silver", "E15ZZPA")], [laptop])
    assert [r["outcome"] for r in rows] == ["insert", "slug"]
