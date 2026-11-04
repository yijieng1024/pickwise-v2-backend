"""
Round 4 Apple repair plan (app/scripts/repair_apple_round4.py). Pure: rows and page
texts in, changes out. Page texts are the specs fixtures test_apple_fields.py uses.
"""
from pathlib import Path

import pytest

from ._adapters import repair_apple_round4 as r4

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"
LR = "https://www.apple.com/my/macbook-pro/liquid-retina-xdr-display/specs/"
NEO = "https://www.apple.com/my/macbook-neo/specs/"
TEXTS = {LR: (FIXTURES / "apple_macbook_pro_liquid_retina_specs.txt").read_text(encoding="utf-8"),
         NEO: (FIXTURES / "apple_macbook_neo_specs.txt").read_text(encoding="utf-8")}
FIELDS = ("max_ram_gb", "weight_kg", "power_supply_details", "external_display_support", "display_resolution",
          "fingerprint_reader", "keyboard_touchpad_details", "media_engine_details")


def _row(key, code, size, chip, cpu, gpu, ram, ssd, **stored):
    return {"id": key + "0000-0000-0000-000000000000", "model_code": code, "status": "inactive",
            "display_size_inch": size, "processor_model": f"Apple {chip} ({cpu}-core)", "cpu_cores": cpu,
            "gpu_cores": gpu, "ram_gb": ram, "ssd_gb": ssd, **{f: None for f in FIELDS}, **stored}


def _rows(**over):
    """The six rows round 4 names, plus *over* replacing any by key."""
    rows = {
        "5c87abdd-": _row("5c87abdd-", "apple-macbook-pro-14-liquid-retina-m5-max-18c-32g-36gb-2048gb",
                          14.2, "M5 Max", 18, 32, 36, 2048, max_ram_gb=128, power_supply_details="70W USB-C Power Adapter"),
        "9d521cf2-": _row("9d521cf2-", "apple-macbook-pro-16-liquid-retina-m5-max-18c-40g-48gb-2048gb",
                          16.2, "M5 Max", 18, 40, 48, 2048, media_engine_details="Hardware-accelerated H.264",
                          max_ram_gb=999),
        "f24b8f91-": _row("f24b8f91-", "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-24gb-2048gb",
                          14.2, "M5 Pro", 15, 16, 24, 2048, power_supply_details="Supports charging via USB-C",
                          display_resolution="3024-by-1964"),
        "5f0c1326-": _row("5f0c1326-", "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-48gb-1024gb",
                          14.2, "M5 Pro", 15, 16, 48, 1024),
        "abc2df6f-": _row("abc2df6f-", "apple-macbook-neo-13-a18-pro-6c-5g-8gb-256gb", 13.0, "A18 Pro", 6, 5, 8, 256,
                          fingerprint_reader=True, keyboard_touchpad_details="Magic Keyboard with Touch ID, Multi-Touch trackpad"),
        "e05aa3e6-": _row("e05aa3e6-", "apple-macbook-pro-14-liquid-retina-m5-pro-18c-20g-24gb-2048gb",
                          14.2, "M5 Pro", 18, 20, 24, 2048, power_supply_details="wrong"),
    }
    rows.update(over)
    return list(rows.values())


def _diff(result, key):
    return next((d for row, d in result["updates"] if row["id"].startswith(key)), {})


def test_5c87abdd_gets_36gb_and_96w_from_the_parser():
    diff = _diff(r4.plan(_rows(), TEXTS), "5c87abdd-")
    assert diff["max_ram_gb"] == (128, 36)
    assert diff["power_supply_details"] == ("70W USB-C Power Adapter", "96W USB-C Power Adapter")


def test_5c87abdd_check_refuses_when_the_parser_disagrees():
    assert "\n36GB unified memory" in TEXTS[LR]
    texts = {**TEXTS, LR: TEXTS[LR].replace("\n36GB unified memory", "\n48GB unified memory")}
    with pytest.raises(r4.Refused, match="5c87abdd- max_ram_gb"):
        r4.plan(_rows(), texts)


def test_build_to_order_row_is_skipped():
    result = r4.plan(_rows(), TEXTS)
    assert _diff(result, "e05aa3e6-") == {}
    assert [s["id"][:9] for s in result["skipped"]] == ["e05aa3e6-"]


def test_build_to_order_exception_changes_only_its_named_field():
    result = r4.plan(_rows(), TEXTS)
    assert _diff(result, "f24b8f91-") == {"power_supply_details": ("Supports charging via USB-C", "70W USB-C Power Adapter")}
    assert set(_diff(result, "9d521cf2-")) == {"media_engine_details"}, "max_ram_gb 999 must stay on a BTO row"


def test_media_engine_comes_from_the_page():
    old, new = _diff(r4.plan(_rows(), TEXTS), "9d521cf2-")["media_engine_details"]
    assert new.startswith("Hardware-accelerated H.264, HEVC, ProRes, and ProRes RAW, Video decode engine, Two video")


def test_a_source_line_missing_from_the_page_refuses():
    texts = {**TEXTS, LR: TEXTS[LR].replace("Two ProRes encode and decode engines", "Two ProRes engines")}
    with pytest.raises(r4.Refused, match="source lines not on the page"):
        r4.plan(_rows(), texts)


def test_neo_256gb_loses_touch_id_and_its_keyboard_wording():
    diff = _diff(r4.plan(_rows(), TEXTS), "abc2df6f-")
    assert diff["fingerprint_reader"] == (True, False)
    assert diff["keyboard_touchpad_details"][1] == "Magic Keyboard, Multi-Touch trackpad"


def test_failed_parse_changes_nothing_and_is_listed():
    m4 = _row("aaaaaaaa-", "apple-macbook-pro-14-liquid-retina-m4-10c-10g-16gb-1024gb", 14.2, "M4", 10, 10, 16, 1024,
              max_ram_gb=24, weight_kg=1.5, power_supply_details="x", external_display_support="y")
    result = r4.plan(_rows(**{"aaaaaaaa-": m4}), TEXTS)
    assert set(_diff(result, "aaaaaaaa-")) == {"display_resolution"}, "only the per-size field parses for an unknown chip"
    [listed] = [p for p in result["parse_failures"] if p["id"].startswith("aaaaaaaa-")]
    assert {f["field"] for f in listed["failures"]} == {"max_ram_gb", "weight_kg", "power_supply_details",
                                                        "external_display_support"}


def test_matching_values_are_not_changed():
    same = _row("5f0c1326-", "apple-macbook-pro-14-liquid-retina-m5-pro-15c-16g-48gb-1024gb", 14.2, "M5 Pro", 15, 16,
                48, 1024, power_supply_details="70W USB-C Power Adapter")
    assert _diff(r4.plan(_rows(**{"5f0c1326-": same}), TEXTS), "5f0c1326-") == {}


def test_a_missing_named_row_refuses():
    rows = [r for r in _rows() if not r["id"].startswith("9d521cf2-")]
    with pytest.raises(r4.Refused, match="named rows not found"):
        r4.plan(rows, TEXTS)
