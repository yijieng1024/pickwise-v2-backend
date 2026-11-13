"""Which laptop a page is another SKU of (plan step 11, decision 15). Pure functions: the
page's own spec table against the catalog's fields, as production stores both."""
from types import SimpleNamespace

import pytest

from tests.unit._adapters import sku_match

HP_PAGE = {
    "Processors / Processors": "Intel® Core™ Ultra 5 322 (up to 4.4 GHz with Intel® Turbo Boost Technology, 8 cores)",
    "Display And Graphics / Graphics": "Intel® Graphics",
    "Memory / Memory": "16 GB LPDDR5x-7467 MT/s (onboard)",
    "Storage / Hard drive description": "512 GB PCIe® Gen4 NVMe™ M.2 SSD",
    "Display And Graphics / Display": '40.6 cm (16") diagonal, 2K (1920 x 1200), IPS',
    "Appearance / Product color": "Starlit blue aluminum",
}
ROG_PAGE = {
    "Processor": "AMD Ryzen™ 9 8940HX Processor 2.4GHz (80MB Cache, up to 5.3GHz, 16 cores)",
    "Graphics": "NVIDIA® GeForce RTX™ 5070 Ti Laptop GPU, ROG Boost: 1565MHz* at 140W",
    "Memory": "16GB DDR5-5200 SO-DIMM",
    "Storage": "1TB PCIe® 4.0 NVMe™ M.2 SSD",
    "Display": "ROG Nebula Display, 16-inch, 2.5K (2560 x 1600, WQXGA) 16:10 aspect ratio",
}


def _laptop(name, cpu, gpu, ram, ssd, screen, laptop_id="a", status="active"):
    return SimpleNamespace(id=laptop_id, product_name=name, processor_model=cpu, gpu_model=gpu, ram_gb=ram,
                           ssd_gb=ssd, display_size_inch=screen, status=status)


@pytest.mark.parametrize("name, line", [
    ("HP OmniBook 5 Flip 2-in-1 14 inch Laptop Next Gen AI 14-km0052TU Silver", "hp omnibook 5 flip 2-in-1"),
    ("HP OmniBook X 14 inch Laptop Next Gen AI 14-ka0074TU Silver", "hp omnibook x"),
    ("HP OmniBook 5 Laptop Next Gen AI 16 inch 16-bp0061AU Silver", "hp omnibook 5"),
    ("HP OmniBook 5 16 inch Laptop Next Gen AI 16-bm0090TU (Intel Core Ultra 5 322, 16GB RAM)", "hp omnibook 5"),
    ("HP ProBook 4 G1i 14 inch Notebook AI PC, Silver + HP Universal USB-C Multiport Hub", "hp probook 4 g1i"),
    ("HP 15.6 inch Laptop 15-fc0957AU Silver", "hp"),
    ("ROG Strix G16 (2025) G614 (G614P-WTS178W)", "rog strix g16"),
    ("ROG Flow Z13-KJP (GZ302E-ACRU168W)", "rog flow z13-kjp"),
])
def test_product_line_is_the_name_before_it_describes_the_machine(name, line):
    assert sku_match.product_line(name) == line


def test_an_hp_page_and_a_rog_page_read_as_the_catalog_stores_them():
    assert sku_match.page_specs(HP_PAGE) == ("322", "intel", 16, 512, 16.0)
    assert sku_match.page_specs(ROG_PAGE) == ("8940hx", "rtx 5070 ti", 16, 1024, 16.0)
    # The catalog's wording of the same two machines, trademark leftovers included
    hp = _laptop("x", "Intel Core Ultra 5 322", "Intel Graphics", 16, 512, 16.0)
    rog = _laptop("x", "AMD Ryzen⁴ 9 8940HX Processor", "NVIDIA® GeForce RTX⁴ 5070 Ti Laptop GPU", 16, 1024, 16.0)
    assert sku_match.laptop_specs(hp) == sku_match.page_specs(HP_PAGE)
    assert sku_match.laptop_specs(rog) == sku_match.page_specs(ROG_PAGE)


@pytest.mark.parametrize("memory, total", [
    ("8 GB DDR4-3200 MT/s (1 x 8 GB)", 8),      # HP: the total comes first
    ("16 GB DDR4-3200 MT/s (2 x 8 GB)", 16),
    ("16GB DDR5-5200 SO-DIMM", 16),
    ("16GB DDR5 SO-DIMM *2", 32),               # two sticks
    ("32GB x 2", 64),
])
def test_memory_is_the_total(memory, total):
    assert sku_match.page_specs({**ROG_PAGE, "Memory": memory}).ram_gb == total


def test_a_page_with_a_spec_missing_or_other_keys_cannot_be_read():
    assert sku_match.page_specs({k: v for k, v in ROG_PAGE.items() if k != "Storage"}) is None
    assert sku_match.page_specs({"Processor & Chipset / Processor Model": "Core i5"}) is None  # Acer
    assert sku_match.page_specs(["a page with a list of specs"]) is None                         # Apple


def test_zbook_pages_spell_their_sections_differently_and_still_read():
    zbook = {k.replace("Memory /", "Memory: /").replace("Display And Graphics /", "Display and graphics: /")
             .replace("Storage /", "Storage: /"): v for k, v in HP_PAGE.items()}
    assert sku_match.page_specs(zbook) == sku_match.page_specs(HP_PAGE)
    assert sku_match.page_colour(HP_PAGE) == "Starlit blue aluminum"
    assert sku_match.page_colour(ROG_PAGE) is None


def test_same_machine_needs_the_line_and_every_spec():
    page = sku_match.page_specs(HP_PAGE)
    laptop = _laptop("HP OmniBook 5 16 inch Laptop Next Gen AI 16-bm0090TU Silver", "Intel Core Ultra 5 322",
                     "Intel Graphics", 16, 512, 16.0)
    name = "HP OmniBook 5 16 inch Laptop Next Gen AI 16-bm0089TU Blue"
    assert sku_match.same_machine(name, page, [laptop]) is laptop
    assert sku_match.same_machine("HP OmniBook X 16 inch Laptop Next Gen AI 16-xx Blue", page, [laptop]) is None
    for field, other in (("ram_gb", 32), ("ssd_gb", 1024), ("display_size_inch", 14.0),
                         ("processor_model", "Intel Core Ultra 7 355"), ("gpu_model", "NVIDIA GeForce RTX 5050")):
        different = SimpleNamespace(**{**vars(laptop), field: other})
        assert sku_match.same_machine(name, page, [different]) is None, field


def test_two_laptops_that_fit_give_the_active_one_then_the_lowest_id():
    page = sku_match.page_specs(HP_PAGE)
    name = "HP OmniBook 5 16 inch Laptop Blue"
    fits = [_laptop(name, "Intel Core Ultra 5 322", "Intel Graphics", 16, 512, 16.0, laptop_id=i, status=s)
            for i, s in (("c", "suspended"), ("b", "active"), ("a", "inactive"))]
    assert sku_match.same_machine(name, page, fits).id == "b"
    assert sku_match.same_machine(name, page, fits[::2]).id == "a"
