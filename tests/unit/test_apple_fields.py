"""
Apple fields read by code (app/processor/apple_fields.py, 10a, decided 2026-11-04).

Fixtures are the specs-page text in backups/apple_specs_raw_2026-11-03_pre-rescrape.json:
the two MacBook Pro pages verbatim (scraped 2026-11-03 by the current scraper). The
Neo fixture is the live Apple MY Neo specs page, fetched once on 2026-11-04 and run
through parse_apple_specs.

Expected values are the rows to fix in the plan: 5c87abdd is max RAM 36 with a 96W
charger, f24b8f91 the 70W charger line, abc2df6f (Neo 256GB) has no Touch ID.
"""
from pathlib import Path

import pytest

from ._adapters import apple_fields, apple_name

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _section(page: str, size: int) -> str:
    sections = apple_name.size_sections(_text(page), "MacBook Pro")
    return apple_fields.section_for(sections, size)


LR, PROLED = "apple_macbook_pro_liquid_retina_specs.txt", "apple_macbook_pro_proled_specs.txt"
NEO = "apple_macbook_neo_specs.txt"


def _config(chip, cpu, gpu, size=14, ssd=1024):
    return {"size": size, "chip": chip, "cpu_cores": cpu, "gpu_cores": gpu, "ram_gb": 24, "ssd_gb": ssd}


def test_m5_max_14_is_36gb_max_and_96w():
    """5c87abdd: the LLM stored max RAM 128 (the 40-core GPU's) and a 70W charger."""
    fields, failures = apple_fields.parse(_section(LR, 14), _config("M5 Max", 18, 32), "MacBook Pro")
    assert failures == []
    assert fields["max_ram_gb"] == 36
    assert fields["power_supply_details"] == "96W USB-C Power Adapter"
    assert fields["weight_kg"] == 1.62, "footnote digit not stripped from 1.62kg8"
    assert fields["display_resolution"] == "3024x1964"
    assert fields["external_display_support"].startswith("Supports up to four external displays")


def test_m5_pro_15_core_charger_is_70w_and_18_core_is_96w():
    """In the Box is keyed by chip and CPU cores: "M5 Pro with 15-core CPU" / "M5 Pro with
    18-core CPU or M5 Max"."""
    pro15, _ = apple_fields.parse(_section(LR, 14), _config("M5 Pro", 15, 16), "MacBook Pro")
    pro18, _ = apple_fields.parse(_section(LR, 14), _config("M5 Pro", 18, 20), "MacBook Pro")
    assert pro15["power_supply_details"] == "70W USB-C Power Adapter"
    assert pro18["power_supply_details"] == "96W USB-C Power Adapter"
    assert (pro15["max_ram_gb"], pro18["max_ram_gb"]) == (48, 64), "memory is keyed by GPU cores"


def test_m6_reads_its_own_lines():
    fields, failures = apple_fields.parse(_section(LR, 14), _config("M6", 12, 12), "MacBook Pro")
    assert failures == []
    assert (fields["max_ram_gb"], fields["weight_kg"]) == (32, 1.55), "153GB/s bandwidth read as memory?"
    assert fields["power_supply_details"] == "70W USB-C Power Adapter"
    assert fields["external_display_support"].startswith("Supports up to two external displays")


def test_16_inch_section_is_its_own():
    fields, failures = apple_fields.parse(_section(LR, 16), _config("M5 Max", 18, 40, size=16), "MacBook Pro")
    assert failures == []
    assert (fields["max_ram_gb"], fields["weight_kg"], fields["display_resolution"]) == (128, 2.15, "3456x2234")
    assert fields["power_supply_details"] == "140W USB-C Power Adapter"


def test_proled_resolution_and_charger_wording():
    fields, failures = apple_fields.parse(_section(PROLED, 14), _config("M5 Pro", 15, 16), "MacBook Pro")
    assert failures == []
    assert fields["display_resolution"] == "3024x1986"
    assert fields["power_supply_details"] == "70W Power Adapter"
    assert fields["weight_kg"] == 1.38


@pytest.mark.parametrize("ssd, touch_id", [(256, False), (512, True)])
def test_neo_touch_id_by_storage_column(ssd, touch_id):
    """abc2df6f: the Neo 256GB has no Touch ID; the 512GB does."""
    config = {"size": 13, "chip": "A18 Pro", "cpu_cores": 6, "gpu_cores": 5, "ram_gb": 8, "ssd_gb": ssd}
    fields, failures = apple_fields.parse(_text(NEO), config, "MacBook Neo")
    assert fields["fingerprint_reader"] is touch_id
    assert (fields["max_ram_gb"], fields["weight_kg"], fields["display_resolution"]) == (8, 1.23, "2408x1506")
    assert fields["external_display_support"].startswith("Supports one external display")
    # The live page has "In the Box" (2026-11-04), after the macOS app list.
    assert fields["power_supply_details"] == "20W USB-C Power Adapter"
    assert failures == []


def test_touch_id_is_not_parsed_off_neo():
    fields, _ = apple_fields.parse(_section(LR, 14), _config("M6", 12, 12), "MacBook Pro")
    assert "fingerprint_reader" not in fields


def test_missing_heading_fails_that_field_only():
    section = _section(LR, 14).replace("\nIn the Box\n", "\nIn the Carton\n")
    fields, failures = apple_fields.parse(section, _config("M6", 12, 12), "MacBook Pro")
    assert fields["power_supply_details"] is None
    assert failures == [{"field": "power_supply_details", "reason": "no 'In the Box' heading"}]
    assert fields["max_ram_gb"] == 32


def test_chip_with_no_line_fails():
    fields, failures = apple_fields.parse(_section(LR, 14), _config("M4", 10, 10), "MacBook Pro")
    assert {f["field"] for f in failures} == {"max_ram_gb", "weight_kg", "power_supply_details",
                                              "external_display_support"}
    assert all(fields[f["field"]] is None for f in failures)
    assert fields["display_resolution"] == "3024x1964", "resolution is per size, not per chip"


def test_two_different_values_fail():
    section = _section(LR, 14).replace("Weight (M6): 1.55kg8", "Weight (M6): 1.55kg8\nWeight (M6): 1.70kg8")
    fields, failures = apple_fields.parse(section, _config("M6", 12, 12), "MacBook Pro")
    assert fields["weight_kg"] is None
    assert [f["field"] for f in failures] == ["weight_kg"]
    assert "more than one value" in failures[0]["reason"]


def test_no_section_for_the_size_fails_every_field():
    fields, failures = apple_fields.parse(None, _config("M6", 12, 12, size=15), "MacBook Pro")
    assert set(fields) == set(apple_fields._PARSERS)
    assert all(v is None for v in fields.values())
    assert {f["reason"] for f in failures} == {"no specs section for 15-inch"}


def test_air_configurable_size_is_not_a_new_column():
    """Air's 24GB column reads "24GB / 24GB unified memory / Configurable to: / 32GB": the
    lone 32GB is an option, not a column opening on 32GB with nothing above 24."""
    air = _text("apple_macbook_air_specs.txt")
    section = apple_fields.section_for(apple_name.size_sections(air, "MacBook Air"), 15)
    config = {"size": 15, "chip": "M5", "cpu_cores": 10, "gpu_cores": 10, "ram_gb": 24, "ssd_gb": 1024}
    fields, failures = apple_fields.parse(section, config, "MacBook Air")
    assert failures == []
    assert fields["max_ram_gb"] == 32
    assert fields["power_supply_details"] == "40W Dynamic Power Adapter with 60W Max"
