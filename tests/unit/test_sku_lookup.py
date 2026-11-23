"""A laptop asked for by its bare SKU (lookup_laptop and the price tool score names the
same way, laptop_lookup.match_text). The catalog writes "(G614P-RRV012W)"; a user types
"G614P-RRV012W". A part of a code ("G614P") names no laptop, and the price tool never
quotes one configuration for a code several carry."""
import uuid
from types import SimpleNamespace

import pytest

from ._adapters import lookup_laptop_tool as lt
from ._adapters import market_price

G16 = uuid.uuid4()
ZEN = uuid.uuid4()


def _row(name, family=G16, status="active", price=9999.0):
    return {"id": uuid.uuid4(), "product_name": name, "brand": "Asus", "status": status, "family_id": family,
            "family_name": "ROG Strix 16" if family == G16 else "Zenbook 14", "display_size_inch": 16.0,
            "release_year": 2025, "price_rm": price, "last_price_rm": 9999.0 if status == "discontinued" else None,
            "last_price_date": "2026-08-15" if status == "discontinued" else None}


ROWS = [
    _row("ROG Strix G16 (2025) G614 (G614P-RRV012W) (AMD Ryzen 9 8940HX, 32GB RAM, 1TB SSD)", status="discontinued"),
    _row("ROG Strix G16 (2025) G614 (G614P-HRV057W) (AMD Ryzen 9 8940HX, 16GB RAM, 1TB SSD)"),
    _row("ROG Strix G16 (2025) G614 (G614P-MTS088WP) (AMD Ryzen 9 8940HX, 16GB RAM, 1TB SSD)"),
    _row("ASUS Zenbook 14 OLED (UX3405); Copilot+ PC (UX3405CA) 16GB", family=ZEN, price=5499.0),
    _row("ASUS Zenbook 14 OLED (UX3405); Copilot+ PC (UX3405CA) 32GB", family=ZEN, price=6299.0),
]


@pytest.mark.parametrize("text, expected", [
    ("ROG Strix G16 (2025) G614 (G614P-RRV012W)", "rog strix g16 2025 g614 g614p-rrv012w"),
    ("G614P-MTS088WP", "g614p-mts088w"),  # -W and -WP are one SKU
    ("G614P-WS5119WS", "g614p-ws5119ws"),  # -WS is its own suffix
    ("HP OmniBook 5 Flip 2-in-1 14", "hp omnibook 5 flip 2-in-1 14"),
    ("Ryzen AI Max+ 395, 32GB RAM - 1TB", "ryzen ai max 395 32gb ram 1tb"),
])
def test_match_text(text, expected):
    assert lt.match_text(text) == expected


def _configs(query):
    out = lt.rank(query, ROWS)
    return out["match"], [c["product_name"] for c in out.get("configurations", [])]


def test_a_bare_sku_finds_the_laptop_whose_name_has_it_in_brackets():
    match, names = _configs("G614P-RRV012W")
    assert match == "single" and names == [ROWS[0]["product_name"]]
    assert lt.rank("G614P-RRV012W", ROWS)["configurations"][0]["no_longer_sold"] is True


@pytest.mark.parametrize("query, row", [("G614P-MTS088W", 2), ("G614P-RRV012WP", 0)])
def test_the_w_and_wp_spellings_of_a_sku_find_the_same_laptop(query, row):
    assert _configs(query) == ("single", [ROWS[row]["product_name"]])


def test_a_part_of_a_code_names_no_laptop():
    assert lt.rank("G614P", ROWS) == {"match": "none"}


def test_a_name_search_still_finds_the_whole_line():
    match, names = _configs("ROG Strix G16")
    assert match == "single" and len(names) == 3


# --- the price tool, on the same rows ---------------------------------------------

class _Exec:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows):
        self.laptops = [SimpleNamespace(id=r["id"], product_name=r["product_name"], brand_id=1, model_code=str(r["id"]),
                                        price_rm=r["price_rm"], status=r["status"], price_checked_at=None)
                        for r in rows]

    def exec(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is market_price.Laptop:
            return _Exec(self.laptops)
        if entity is market_price.LaptopBrand:
            return _Exec([SimpleNamespace(id=1, name="Asus")])
        return _Exec([])


def _price(query):
    return market_price._catalog_lookup(query, None, _Session(ROWS))


def test_the_price_tool_finds_a_bare_sku_and_says_it_is_discontinued():
    found = _price("G614P-RRV012W")
    assert found["product_name"] == ROWS[0]["product_name"] and found["no_longer_sold"] is True


def test_the_price_tool_does_not_quote_one_configuration_for_a_shared_code():
    assert _price("G614P") == {"found": False}
    shared = _price("UX3405")
    assert shared["found"] is False and shared["ambiguous"] is True
    assert shared["candidates"] == sorted([ROWS[3]["product_name"], ROWS[4]["product_name"]])


def test_the_price_tool_still_gives_one_row_for_a_plain_name():
    found = _price("Zenbook 14 OLED 32GB")
    assert found["found"] is True and found["product_name"] == ROWS[4]["product_name"]
