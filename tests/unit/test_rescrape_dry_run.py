"""What a ROG and HP re-scrape would do. `plan` decides everything, so no database and no
fetch. The match itself is app/laptops/sku_match.py, tested in test_sku_match.py."""
from types import SimpleNamespace

from tests.unit._adapters import rescrape_plan as plan

HP = "hp-brand"
PAGE = {
    "Processors / Processors": "Intel® Core™ Ultra 5 322 (up to 4.4 GHz)",
    "Display And Graphics / Graphics": "Intel® Graphics",
    "Memory / Memory": "16 GB LPDDR5x-7467 MT/s (onboard)",
    "Storage / Hard drive description": "512 GB PCIe® Gen4 NVMe™ M.2 SSD",
    "Display And Graphics / Display": '40.6 cm (16") diagonal, 2K',
    "Price": "RM5,099.00",
}
NAME = "HP OmniBook 5 16 inch Laptop Next Gen AI 16-bm0090TU Silver"


def _laptop(name=NAME, sku=None, laptop_id="a", brand=HP, ram=16, skus=()):
    return SimpleNamespace(id=laptop_id, brand_id=brand, product_name=name, model_code=f"slug-{laptop_id}",
                           manufacturer_sku=sku, status="active", price_rm=5099.0,
                           processor_model="Intel Core Ultra 5 322", gpu_model="Intel Graphics", ram_gb=ram,
                           ssd_gb=512, display_size_inch=16.0, skus=list(skus))


def _variant(sku, name=NAME, stored="changed", brand=HP, features=None, url="https://www.hp.com/p.html"):
    return SimpleNamespace(source_url=url, brand_id=brand, name=name, sku=sku, stored=stored,
                           features=PAGE if features is None else features)


def _outcomes(variants, laptops):
    return [(row["outcome"], row["laptop"].id if row["laptop"] else None) for row in plan(variants, laptops)]


def test_an_unchanged_page_is_not_processed_whatever_its_sku():
    assert _outcomes([_variant("E0GP5PA", stored="unchanged")], [_laptop(sku="E0GP5PA")]) == [("unchanged", None)]


def test_a_stored_page_fetched_with_no_sku_keeps_its_row():
    assert _outcomes([_variant(None)], [_laptop()]) == [("no SKU", None)]


def test_a_laptop_holding_the_sku_is_updated_and_w_reads_as_wp():
    rog = _laptop("ROG Strix G16 (G614P-MTS088WP)", sku="G614P-MTS088WP", brand="asus")
    assert _outcomes([_variant("G614P-MTS088W", name="Any other name", brand="asus")], [rog]) == [("SKU", "a")]


def test_a_sku_the_laptop_carries_beside_its_own_is_found_too():
    assert _outcomes([_variant("E0GP3PA", name="Any other name")], [_laptop(sku="E0GP5PA", skus=["E0GP3PA"])]) == [
        ("SKU", "a")]


def test_colour_pages_of_one_machine_all_attach_to_its_laptop():
    laptop = _laptop()
    rows = plan([_variant("E0GP3PA", name=NAME.replace("90TU Silver", "89TU Blue")),
                 _variant("E0GP5PA"), _variant("E0GP6PA")], [laptop])
    assert [(r["outcome"], r["laptop"].id) for r in rows] == [("attach", "a")] * 3
    assert laptop.skus == [], "plan changed the laptops it was given"


def test_another_product_line_or_other_specs_is_an_insert_with_the_closest_listed():
    other_line = _variant("DP8F9PA", name="HP OmniBook X 16 inch Laptop Next Gen AI 16-xx0001TU Silver")
    other_ram = _variant("E0GP9PA", features={**PAGE, "Memory / Memory": "32 GB LPDDR5x (onboard)"})
    assert _outcomes([other_line], [_laptop()]) == [("insert", "a")]
    assert _outcomes([other_ram], [_laptop()]) == [("insert", "a")]


def test_the_second_page_of_a_new_machine_attaches_to_the_laptop_of_the_first():
    rows = plan([_variant("AAA1PA"), _variant("AAA2PA")], [_laptop(ram=8)])
    assert [r["outcome"] for r in rows] == ["insert", "attach"]
    assert rows[1]["laptop"].id == "new laptop of AAA1PA"


def test_a_page_whose_specs_cannot_be_read_goes_by_slug():
    assert _outcomes([_variant("E0GP5PA", features={"Price": "RM1.00"})], [_laptop()]) == [("slug", None)]


def test_only_laptops_of_the_same_brand_are_candidates():
    assert _outcomes([_variant("E0GP5PA")], [_laptop(brand="asus")]) == [("insert", None)]
