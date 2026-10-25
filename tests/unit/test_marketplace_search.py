"""Shopee / Lazada search text: real production product names (2026-10-25)."""
import pytest

from tests.unit._adapters import marketplace

marketplace_links, search_text = marketplace.marketplace_links, marketplace.search_text

CASES = [
    # Asus: chassis code, the part before the hyphen
    (("Asus", "ROG Zephyrus G16 (2025) GU605 (GU605C-PQR045W)", "Intel Core Ultra 9 285H", "NVIDIA GeForce RTX 5070 Laptop GPU"),
     "Asus ROG Zephyrus G16 GU605"),
    (("Asus", "ROG Strix SCAR 18 (2025) G835 (G835L-WSA154W)", "Intel Core Ultra 9 275HX", "NVIDIA GeForce RTX 5080 Laptop GPU"),
     "Asus ROG Strix SCAR 18 G835"),
    (("Asus", "ASUS Vivobook S14 (S3407QA) Copilot+ PC", "Snapdragon X X1-26-100", "Qualcomm Adreno"),
     "ASUS Vivobook S14 S3407QA"),
    (("Asus", "ASUS ExpertBook PM3 (PM3406) - Ryzen AI 5 330, 16GB RAM, 512GB SSD", "AMD Ryzen AI 5 330", "AMD Radeon 820M"),
     "ASUS ExpertBook PM3 PM3406"),
    # HP / Acer: full SKU
    (("HP", "HP OmniBook 7 14 inch Laptop AI 14-fr0020TU Pink", "Intel Core Ultra 5 225H", "Intel Arc Graphics"),
     "HP OmniBook 7 14-fr0020TU"),
    (("HP", "HP 15.6 inch Laptop 15-fc0956AU Gold", "AMD Ryzen 7 7730U", "AMD Radeon Graphics"),
     "HP 15-fc0956AU"),
    (("Acer", "Acer Aspire 7 Gaming (A715-59G-54Q6, Intel Core 5 210H, 8GB RAM, 512GB SSD)", "Intel Core 5 210H", "NVIDIA GeForce RTX 3050"),
     "Acer Aspire 7 Gaming A715-59G-54Q6"),
    # no code in the name: product-line head + CPU
    (("HP", "HP EliteBook 8 G2i 14 inch Notebook Next Gen AI PC (Intel Core Ultra 5 325, 32GB RAM, 512GB SSD)", "Intel Core Ultra 5 325", "Intel Graphics"),
     "HP EliteBook 8 G2i Core Ultra 5 325"),
    (("Acer", "Predator Helios Neo 16S Ai (Intel Core Ultra 7 255HX, GeForce RTX 5070, 1TB SSD)", "Intel Core Ultra 7 255HX", "NVIDIA GeForce RTX 5070"),
     "Acer Predator Helios Neo 16S Core Ultra 7 255HX"),
]


@pytest.mark.parametrize("args,expected", CASES)
def test_search_text(args, expected):
    assert search_text(*args) == expected


def test_apple_uses_family_and_chip():
    name = "16-inch MacBook Pro (M5 Max, 18-core CPU, 40-core GPU, 48GB RAM, 2TB SSD)"
    assert search_text("Apple", name, "Apple M5 Max", "Apple M5 Max", "Apple MacBook Pro 16") == "Apple MacBook Pro 16 M5 Max"


def test_ram_and_storage_never_in_the_query():
    q = search_text("Acer", "Acer Nitro V 15 Gaming Laptop (Intel Core 5 210H, RTX 4050, 16GB RAM, 512GB SSD)",
                    "Intel Core 5 210H", "NVIDIA GeForce RTX 4050")
    assert "GB" not in q and q == "Acer Nitro V 15 Gaming Core 5 210H"


def test_links_are_url_encoded():
    links = marketplace_links("HP 15-fc0956AU")
    assert links["shopee_search_url"] == "https://shopee.com.my/search?keyword=HP+15-fc0956AU"
    assert links["lazada_search_url"] == "https://www.lazada.com.my/catalog/?q=HP+15-fc0956AU"
