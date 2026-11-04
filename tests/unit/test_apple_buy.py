"""Apple buy pages (decided 2026-11-03): configurations and prices from the buy page,
decided in code. Parsers on small pages shaped like the real ones; the plan on plain
rows. No database, no network."""
from ._adapters import apple_buy, apple_name, apple_scraper

PRO = "https://www.apple.com/my/shop/buy-mac/macbook-pro"
NEO = "https://www.apple.com/my/shop/buy-mac/macbook-neo"
SILVER = "14-inch-silver-standard-display-apple-m5-pro-chip-15-core-cpu-16-core-gpu-24gb-memory-1tb-storage"
BLACK = SILVER.replace("silver", "space-black")
NANO = SILVER.replace("standard-display", "nano-texture-display")
CODE = "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-1024gb"


def _links(page: str, *slugs: str, extra: str = "") -> str:
    return "<html><title>Buy MacBook Pro - Apple (MY)</title>" + extra + "".join(
        f'<a href="/my/shop/buy-mac/{page}/{s}">x</a>' for s in slugs) + "</html>"


def test_slug_gives_every_identity_field():
    assert apple_name.parse_config_slug(SILVER) == {
        "size": 14, "chip": "M5 Pro", "cpu_cores": 15, "gpu_cores": 16, "ram_gb": 24, "ssd_gb": 1024,
        "nano_texture": False,
    }
    assert apple_name.parse_config_slug("silver-256gb")["ssd_gb"] == 256


def test_buy_page_folds_colours_and_skips_nano_texture():
    page = apple_scraper.parse_buy_page(_links("macbook-pro", SILVER, BLACK, NANO), PRO)
    [config] = page["configs"]
    assert config["slugs"] == [SILVER, BLACK], "each colour became its own configuration"
    assert page["nano_texture"] == [NANO]


def test_neo_cores_come_from_the_page():
    dims = '"processor-cpuCoreCount-gpuCoreCount":"6-5"'
    page = apple_scraper.parse_buy_page(_links("macbook-neo", "silver-256gb", "indigo-256gb", extra=dims), NEO)
    [config] = page["configs"]
    assert (config["cpu_cores"], config["gpu_cores"], config["ssd_gb"]) == (6, 5, 256)


def test_detail_page_part_number_and_price():
    html = ('"products":[{"sku":"MGDN4","partNumber":"MGDN4ZP/A","price":{"fullPrice":10299.00},'
            '"category":"macbook_pro","name":"14-inch MacBook Pro - Silver"}]')
    assert apple_scraper.parse_detail_page(html) == {
        "part_number": "MGDN4ZP/A", "price_rm": 10299.0, "name": "14-inch MacBook Pro - Silver"}


def test_resolve_reads_what_neo_links_leave_out():
    neo = {"size": None, "chip": None, "cpu_cores": 6, "gpu_cores": 5, "ram_gb": None, "ssd_gb": 256}
    # As the stored Neo page has it: no-break spaces, the memory once per storage column
    text = "Chip\nApple\xa0A18\xa0Pro\xa0chip\nMemory\n8GB unified\xa0memory\n8GB unified\xa0memory\n13-inch"
    [config], unresolved = apple_buy.resolve([neo], [text])
    assert unresolved == []
    assert (config["chip"], config["ram_gb"], config["size"]) == ("A18 Pro", 8, 13)
    _, [left] = apple_buy.resolve([neo], ["Apple A18 Pro chip\n8GB unified memory\n16GB unified memory\n13-inch"])
    assert left["missing"] == ["ram_gb"], "a page naming two memory sizes was read as one"


def _config(**over):
    config = apple_name.parse_config_slug(SILVER)
    config.pop("nano_texture")
    return {**config, "part_numbers": ["MGDN4ZP/A", "MGDR4ZP/A"], "price_rm": 10299.0, **over}


def _row(id, code=CODE, status="active", price=9999.0, parts=None, size=14.2, chip="Apple M5 Pro (15-core)"):
    return {"id": id, "model_code": code, "status": status, "part_numbers": parts, "price_rm": price,
            "display_size_inch": size, "processor_model": chip}


def _plan(rows, sources=None, configs=None):
    return apple_buy.plan(configs or [_config()], "MacBook Pro", "liquid-retina", rows, sources or {})


def test_new_config_is_an_insert():
    result = _plan([])
    assert [i["model_code"] for i in result["inserts"]] == [CODE]


def test_part_number_matches_before_model_code():
    rows = [_row("by-code"), _row("by-part", code="apple-macbook-pro-14-liquid-retina-old", parts=["MGDR4ZP/A"])]
    [update] = _plan(rows)["updates"]
    assert update["laptop_id"] == "by-part"


def test_unsourced_price_is_overwritten_and_listed():
    result = _plan([_row("a")], {"a": None})
    assert result["updates"][0]["write_price"] is True
    assert result["price_changes"] == [
        {"laptop_id": "a", "model_code": CODE, "status": "active", "old": 9999.0, "new": 10299.0}]


def test_guide_price_is_not_overwritten():
    result = _plan([_row("a")], {"a": "guide"})
    assert result["updates"][0]["write_price"] is False
    assert result["price_changes"] == []
    assert result["prices_not_overwritten"][0]["source"] == "guide"


def test_suspended_match_is_listed_not_updated():
    result = _plan([_row("s", status="suspended")])
    assert result["updates"] == [] and result["inserts"] == []
    assert result["matched_suspended"] == [{"laptop_id": "s", "model_code": CODE}]


def test_identity_conflict_is_not_updated():
    result = _plan([_row("a", size=16.2)])
    assert result["updates"] == []
    assert result["identity_conflicts"][0]["existing"]["screen_inch"] == 16


def test_rows_not_on_the_buy_page_are_listed_by_page():
    rows = [_row("a"), _row("cto", code=CODE.replace("24gb", "48gb")),
            _row("proled", code=CODE.replace("liquid-retina", "proled")),
            _row("air", code="apple-macbook-air-13-m5-10c-8g-16gb-512gb")]
    result = _plan(rows)
    assert [r["laptop_id"] for r in result["not_on_buy_page"]] == ["cto"], (
        "a row from another page was listed, or the build-to-order row was not")


def test_discontinued_match_is_relisted_and_updated():
    """On sale again (ADR-0009 Amendment II): updated, marked to go back to active, and
    listed in relisted. A suspended match is still left alone (test above)."""
    result = _plan([_row("d", status="discontinued")])
    [update] = result["updates"]
    assert (update["laptop_id"], update["relist"]) == ("d", True)
    assert result["relisted"] == [{"laptop_id": "d", "model_code": CODE}]
    assert result["matched_suspended"] == []


def test_active_match_is_not_relisted():
    result = _plan([_row("a")])
    assert result["updates"][0]["relist"] is False and result["relisted"] == []
