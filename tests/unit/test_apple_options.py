"""Apple configurator responses (step 10c). Fixtures are the real responses from the
2026-11-04 trial on the MacBook Air 13 (M5, 10-core CPU / 8-core GPU, 16GB, 512GB,
RM 5,499). No database, no network."""
import copy
import json
from pathlib import Path

import pytest

from ._adapters import apple_options

_DIR = Path(__file__).parent / "fixtures" / "scraper"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text(encoding="utf-8"))


# Sent as the standard kit with 1TB requested; Apple reset it, so deltas are already
# relative to the standard config.
STANDARD = _load("apple_update_config_air13_standard.json")
# Sent as a 24GB / 1TB kit: Apple's deltas are relative to that kit (24GB is 0, 16GB -900).
FROM_24GB = _load("apple_update_config_air13_24gb.json")
SWITCH_24GB = _load("apple_dependency_switcher_air13_24gb.json")
SWITCH_70W = _load("apple_dependency_switcher_air13_70w.json")


def _by_key(options):
    return {(o.group, o.value): o for o in options}


def _add_ons(options, group):
    return {o.value: o.add_on_rm for o in options if o.group == group}


def test_add_ons_are_relative_to_the_standard_config_whichever_kit_was_sent():
    expected_memory = {"16gb": 0.0, "24gb": 900.0, "32gb": 1800.0}
    expected_storage = {"512gb": 0.0, "1tb": 1350.0, "2tb": 3600.0, "4tb": 8100.0}
    for payload in (STANDARD, FROM_24GB):
        options = apple_options.parse_update_config(payload)
        assert _add_ons(options, "memory") == expected_memory
        assert _add_ons(options, "storage") == expected_storage
        assert _add_ons(options, "adapter") == {"40w": 0.0, "35w": 50.0, "70w": 50.0}


def test_unpriced_groups_have_no_add_on():
    options = apple_options.parse_update_config(STANDARD)
    assert set(_add_ons(options, "chip").values()) == {None}
    assert set(_add_ons(options, "keyboard").values()) == {None}
    assert {o.group for o in options} == {"chip", "memory", "storage", "adapter", "keyboard"}


def test_included_options_are_the_standard_configs_own():
    for payload in (STANDARD, FROM_24GB):
        included = {(o.group, o.value) for o in apple_options.parse_update_config(payload) if o.is_included}
        assert included == {("chip", "10-8"), ("memory", "16gb"), ("storage", "512gb"),
                            ("adapter", "40w"), ("keyboard", "none")}


def test_blocked_options_are_flagged():
    # The trial saw no blocked option on this machine, so one is marked by hand.
    payload = copy.deepcopy(STANDARD)
    payload["body"]["options"]["storage-dimensionCapacity"]["compatibleOptions"]["4tb"]["isBlocked"] = True
    options = _by_key(apple_options.parse_update_config(payload))
    assert options[("storage", "4tb")].is_blocked
    assert [k for k, o in options.items() if o.is_blocked] == [("storage", "4tb")]


def test_24gb_on_the_13_inch_forces_the_10_core_gpu_chip():
    rules = apple_options.parse_dependency_switcher(SWITCH_24GB, "memory", "24gb")
    assert rules == [apple_options.Rule("memory", "24gb", "chip", "10-10")]


def test_an_option_with_no_forced_change_gives_no_rule():
    assert apple_options.parse_dependency_switcher(SWITCH_70W, "adapter", "70w") == []


@pytest.mark.parametrize("payload", [
    None,
    "<html>Access Denied</html>",
    {"head": {"status": "200"}, "body": {}},
    {"body": {"options": {}}},
])
def test_malformed_update_config_fails_cleanly(payload):
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_update_config(payload)


def test_unreadable_price_fails_cleanly():
    payload = copy.deepcopy(STANDARD)
    payload["body"]["options"]["memory-dimensionMemory"]["compatibleOptions"]["24gb"]["priceDelta"] = "RM 900"
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_update_config(payload)


@pytest.mark.parametrize("payload", [None, {"body": {}}, {"body": {"overlayContent": {"currentKit": {}}}}])
def test_malformed_dependency_switcher_fails_cleanly(payload):
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_dependency_switcher(payload, "memory", "24gb")


def test_a_switcher_answer_for_another_choice_fails_cleanly():
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_dependency_switcher(SWITCH_24GB, "memory", "32gb")
