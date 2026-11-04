"""
lookup_laptop (app/agent/tools/laptop_lookup.py, 10b step c, decided 2026-11-04).

Rows are modelled on the production names in the 2026-11-04 survey. The last test pins
the market price tool's catalog layer, which these changes must not reach.
"""
import uuid
from types import SimpleNamespace

from ._adapters import lookup_laptop_tool as lt
from ._adapters import market_price

FAM = {k: uuid.uuid4() for k in ("air13", "air15", "pro14", "pro14p", "pro16", "pro16p", "f16", "a16")}


def _row(name, fam, size, status="active", brand="Apple", year=2026, price=5499.0, last=None, date=None):
    return {"id": uuid.uuid4(), "product_name": name, "brand": brand, "status": status, "family_id": FAM[fam],
            "family_name": fam, "display_size_inch": size, "release_year": year, "price_rm": price,
            "last_price_rm": last, "last_price_date": date}


ROWS = [
    _row("13-inch MacBook Air (M5, 10-core CPU, 8-core GPU, 16GB RAM, 512GB SSD)", "air13", 13.6),
    _row("13-inch MacBook Air (M5, 10-core CPU, 10-core GPU, 24GB RAM, 512GB SSD)", "air13", 13.6),
    _row("15-inch MacBook Air (M5, 10-core CPU, 10-core GPU, 16GB RAM, 1TB SSD)", "air15", 15.3),
    _row("14-inch MacBook Pro Liquid Retina XDR (M6, 12-core CPU, 12-core GPU, 16GB RAM, 1TB SSD)", "pro14", 14.2),
    _row("14-inch MacBook Pro Liquid Retina XDR (M5, 10-core CPU, 10-core GPU, 24GB RAM, 1TB SSD)", "pro14", 14.2,
         status="discontinued", last=9199.0, date="2026-06-25"),
    _row("14-inch MacBook Pro PROLED XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 1TB SSD)", "pro14p", 14.2),
    _row("16-inch MacBook Pro Liquid Retina XDR (M5 Pro, 18-core CPU, 20-core GPU, 24GB RAM, 1TB SSD)", "pro16", 16.2),
    _row("16-inch MacBook Pro PROLED XDR (M5 Max, 18-core CPU, 40-core GPU, 48GB RAM, 2TB SSD)", "pro16p", 16.2),
    _row("ASUS TUF Gaming F16 (2025) (FX608JHI) (Intel Core i5-14450HX, 16GB RAM, 512GB SSD)", "f16", 16.0,
         brand="Asus", year=2025),
    _row("ASUS TUF Gaming F16 (2024) (FX607JV) (Intel Core i7-13650HX, 16GB RAM, 1TB SSD)", "f16", 16.0,
         brand="Asus", year=2024, status="discontinued"),
    _row("ASUS TUF Gaming A16 (2025) (FA608PM) (AMD Ryzen 9 8940HX, 16GB RAM, 1TB SSD)", "a16", 16.0,
         brand="Asus", year=2025),
    _row("ASUS TUF Gaming A16 (2024) (FA607NUQ) (AMD Ryzen 7 170, 16GB RAM, 512GB SSD)", "a16", 16.0,
         brand="Asus", year=2024),
    _row("ASUS TUF Gaming A16 Advantage Edition (2023) (FA617NSR)", "a16", 16.0, brand="Asus", year=2023,
         status="discontinued", last=5999.0, date="2026-06-22"),
    _row("ASUS TUF Gaming A16 (2025) (FA608UH) (AMD Ryzen 7 260, 16GB RAM, 512GB SSD)", "a16", 16.0,
         brand="Asus", year=2025, status="suspended"),
]


def test_a_year_is_a_tie_breaker_not_part_of_the_score():
    """Scored with "2023" in it, "TUF A16 2023" fell below the floor and found nothing."""
    result = lt.rank("TUF A16 2023", ROWS)
    assert result["match"] == "single"
    [config] = result["configurations"]
    assert config["product_name"].startswith("ASUS TUF Gaming A16 Advantage Edition (2023)")
    assert config["no_longer_sold"] is True
    assert (config["last_official_price_rm"], config["last_official_price_date"]) == (5999.0, "2026-06-22")


def test_a_screen_size_keeps_other_sizes_out():
    result = lt.rank("MacBook Pro 14", ROWS)
    assert result["match"] == "candidates"
    assert sorted(c["family"] for c in result["candidates"]) == ["pro14", "pro14p"]


def test_two_product_lines_are_two_candidates():
    result = lt.rank("MacBook Air M5", ROWS)
    assert result["match"] == "candidates"
    assert sorted(c["family"] for c in result["candidates"]) == ["air13", "air15"]


def test_one_product_line_is_a_single_answer_with_its_configurations():
    result = lt.rank("TUF F16", ROWS)
    assert result["match"] == "single" and result["family"] == "f16"
    assert {c["status"] for c in result["configurations"]} == {"active", "discontinued"}


def test_suspended_and_inactive_are_never_found():
    names = [c["product_name"] for c in lt.rank("TUF A16", ROWS)["configurations"]]
    assert not any("FA608UH" in n for n in names), "a suspended row was found"


def test_nothing_close_is_none():
    assert lt.rank("Surface Laptop 7", ROWS) == {"match": "none"}


class _Exec:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """What _catalog_lookup reads: laptops, brands, history (none)."""
    def __init__(self, laptops, brands):
        self.laptops, self.brands = laptops, brands

    def exec(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is market_price.Laptop:
            return _Exec(self.laptops)
        if entity is market_price.LaptopBrand:
            return _Exec(self.brands)
        return _Exec([])


def test_market_price_catalog_layer_is_unchanged():
    """lookup_laptop's year and size rules live in lookup_laptop only: the price tool
    still scores the year ("TUF A16 2023" stays below its floor) and still returns one
    best row for an ambiguous name."""
    brand = SimpleNamespace(id=1, name="Asus")
    laptops = [SimpleNamespace(id=r["id"], product_name=r["product_name"], brand_id=1, model_code="x",
                               price_rm=r["price_rm"]) for r in ROWS if r["brand"] == "Asus"]
    session = _Session(laptops, [brand])
    assert market_price._catalog_lookup("TUF A16 2023", None, session) == {"found": False}
    found = market_price._catalog_lookup("TUF F16", None, session)
    assert found["found"] is True and "candidates" not in found
