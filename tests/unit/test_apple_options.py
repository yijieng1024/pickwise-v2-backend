"""Apple configurator responses (step 10c). Fixtures are real responses from 2026-11-04 on
the MacBook Air 13 (M5): its 10-8 / 16GB / 512GB base (RM 5,499) and its 10-10 / 16GB / 1TB
standard config (RM 6,849), each sent as itself. No database, no network."""
import copy
import json
from pathlib import Path

import pytest

from ._adapters import apple_options

_DIR = Path(__file__).parent / "fixtures" / "scraper"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text(encoding="utf-8"))


# The 10-8 base: update-config lists only 16GB and 512GB, the rest needs the 10-10 chip.
STANDARD = _load("apple_update_config_air13_standard.json")
# The 10-10 / 1TB standard config: Apple marks the base's 512GB `isDefault`, its own 1TB
# `isSelected`, and prices relative to 1TB (512GB is -1,350).
ONE_TB = _load("apple_update_config_air13_1tb.json")
SWITCH_24GB = _load("apple_dependency_switcher_air13_24gb.json")
SWITCH_70W = _load("apple_dependency_switcher_air13_70w.json")


def _by_key(options):
    return {(o.group, o.value): o for o in options}


def _add_ons(options, group):
    return {o.value: o.add_on_rm for o in options if o.group == group}


def test_add_ons_are_relative_to_the_standard_config_sent():
    options = apple_options.parse_update_config(ONE_TB)
    assert _add_ons(options, "memory") == {"16gb": 0.0, "24gb": 900.0, "32gb": 1800.0}
    assert _add_ons(options, "storage") == {"512gb": -1350.0, "1tb": 0.0, "2tb": 2250.0, "4tb": 6750.0}
    assert _add_ons(options, "adapter") == {"40w": 0.0, "35w": 50.0, "70w": 50.0}


def test_the_base_lists_only_what_its_chip_takes():
    options = apple_options.parse_update_config(STANDARD)
    assert _add_ons(options, "memory") == {"16gb": 0.0}
    assert _add_ons(options, "storage") == {"512gb": 0.0}


def test_unpriced_groups_have_no_add_on():
    options = apple_options.parse_update_config(STANDARD)
    assert set(_add_ons(options, "chip").values()) == {None}
    assert set(_add_ons(options, "keyboard").values()) == {None}
    assert {o.group for o in options} == {"chip", "memory", "storage", "adapter", "keyboard"}


def test_included_options_are_the_standard_configs_own_not_apples_default():
    included = {(o.group, o.value) for o in apple_options.parse_update_config(ONE_TB) if o.is_included}
    assert included == {("chip", "10-10"), ("memory", "16gb"), ("storage", "1tb"),
                        ("adapter", "40w"), ("keyboard", "none")}


def test_blocked_options_are_flagged():
    # No real response had a blocked option, so one is marked by hand.
    payload = copy.deepcopy(ONE_TB)
    payload["body"]["options"]["storage-dimensionCapacity"]["compatibleOptions"]["4tb"]["isBlocked"] = True
    options = _by_key(apple_options.parse_update_config(payload))
    assert options[("storage", "4tb")].is_blocked
    assert [k for k, o in options.items() if o.is_blocked] == [("storage", "4tb")]


def test_24gb_on_the_13_inch_forces_the_10_core_gpu_chip_for_900():
    switch = apple_options.parse_dependency_switcher(SWITCH_24GB, "memory", "24gb")
    assert switch.rules == [apple_options.Rule("memory", "24gb", "chip", "10-10")]
    assert switch.add_on_rm == 900.0, "RM 6,399 against the base's RM 5,499"


def test_an_option_with_no_forced_change_gives_no_rule():
    switch = apple_options.parse_dependency_switcher(SWITCH_70W, "adapter", "70w")
    assert switch.rules == [] and switch.add_on_rm == 50.0


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
    payload = copy.deepcopy(ONE_TB)
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


# --------------------------------------------------------------------------- step (c)

from tests._apple_fake import AIR13_SLUG, AIR_BUY, FakeApple, fixture  # noqa: E402

from ._adapters import apple_fields, apple_name, apple_options_fetch  # noqa: E402

AIR_PAGE = fixture("apple_configure_air13.html")
PRO_PAGE = fixture("apple_configure_pro14_proled.html")


def test_configure_page_gives_the_kit_the_endpoints_and_the_labels():
    air = apple_options.parse_configure_page(AIR_PAGE)
    assert air.kit["memory-dimensionMemory"] == "16gb" and air.kit["processor-cpuCoreCount-gpuCoreCount"] == "10-8"
    assert air.update_path == "/my/shop/api/cto/update-config?collection=MBA_2026_COLLECTION&fae=true"
    assert air.labels[("memory", "24gb")] == "24GB"
    assert air.labels[("chip", "10-10")] == "10-core CPU, 10-core GPU, 16-core Neural Engine"
    assert air.labels[("adapter", "40w")] == "40W Dynamic Power Adapter with 60W Max"
    pro = apple_options.parse_configure_page(PRO_PAGE)
    assert pro.labels[("chip", "m5max-18-40")] == "18-core CPU, 40-core GPU"
    assert pro.labels[("display", "nano_texture")] == "Nano-texture glass"


def test_a_page_with_no_kit_fails_cleanly():
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_configure_page("<html>Access Denied</html>")


def test_a_chip_change_on_the_pro_also_names_the_chip():
    kit = apple_options.parse_configure_page(PRO_PAGE).kit
    q = apple_options.query(kit, "chip", "m5max-18-40")
    assert "uv.processor-dimensionChip-cpuCoreCount-gpuCoreCount=m5max-18-40" in q
    assert "uv.processor-dimensionChip=m5max" in q
    assert "sv.memory-dimensionMemory=24gb" in q


def test_chip_strings_come_from_the_specs_page_in_the_catalog_form():
    air = apple_options.chip_strings(fixture("apple_macbook_air_specs.txt"))
    assert air[(10, 10)] == ("Apple M5 (10-core)", "10-core GPU")
    pro = apple_options.chip_strings(fixture("apple_macbook_pro_proled_specs.txt"))
    assert pro[(18, 40)] == ("Apple M5 Max (18-core)", "40-core GPU")
    shared = "Apple M5 Pro chip\n18-core CPU\n20-core GPU\nApple M5 Max chip\n18-core CPU\n20-core GPU\n"
    assert apple_options.chip_strings(shared) == {}, "two chips with the same cores were guessed"


def _air_target(**overrides):
    sections = apple_name.size_sections(fixture("apple_macbook_air_specs.txt"), "MacBook Air")
    return {"laptop_id": "air13", "model_code": "apple-macbook-air-13-m5-10c-8g-16gb-512gb", "status": "active",
            "url": f"{AIR_BUY}/{AIR13_SLUG}", "size": 13, "section": apple_fields.section_for(sections, 13), **overrides}


def test_one_machine_gets_every_option_rule_label_and_chip_string():
    fake = FakeApple()
    fetched = apple_options_fetch.fetch_all([_air_target()], fake, pause_s=0)
    machine = fetched["machines"]["air13"]
    by_key = {(o["option_group"], o["value"]): o for o in machine["options"]}
    assert len(by_key) == 18
    assert by_key[("storage", "1tb")]["add_on_rm"] == 1350.0 and by_key[("storage", "1tb")]["label"] == "1TB"
    assert by_key[("chip", "10-10")]["cpu_model"] == "Apple M5 (10-core)"
    assert by_key[("chip", "10-10")]["gpu_model"] == "10-core GPU"
    triggers = [("memory", "24gb"), ("memory", "32gb"), ("storage", "1tb"), ("storage", "2tb"), ("storage", "4tb")]
    assert sorted((r["trigger_group"], r["trigger_value"]) for r in machine["rules"]) == sorted(triggers)
    assert {(r["forced_group"], r["forced_value"]) for r in machine["rules"]} == {("chip", "10-10")}
    assert machine["labels_missing"] == [] and machine["chip_not_offered"] == []
    # The chip option is asked too (on the Pro a chip can force memory); here it forces nothing.
    assert fetched["calls"] == 8 == len(fake.calls), "configure page + update-config + 6 switcher calls"


def _section(specs: str, product: str, size: int):
    return apple_fields.section_for(apple_name.size_sections(fixture(specs), product), size)


def test_a_machine_whose_own_chip_is_not_on_its_specs_page_fails():
    fetched = apple_options_fetch.fetch_all([_air_target(section=None)], FakeApple(), pause_s=0)
    assert fetched["machines"] == {}
    assert "own chip" in fetched["options_failed"][0]["reason"]


@pytest.mark.parametrize("specs, product, size, kept, dropped", [
    ("apple_macbook_air_specs.txt", "MacBook Air", 15, ["10-10"], ["10-8"]),
    ("apple_macbook_air_specs.txt", "MacBook Air", 13, ["10-8", "10-10"], []),
    ("apple_macbook_pro_liquid_retina_specs.txt", "MacBook Pro", 16,
     ["m5pro-18-20", "m5max-18-32", "m5max-18-40"], ["m5pro-15-16", "m6-12-12"]),
    ("apple_macbook_pro_liquid_retina_specs.txt", "MacBook Pro", 14,
     ["m6-12-12", "m5pro-15-16", "m5pro-18-20"], []),
])
def test_a_chip_counts_only_where_its_sizes_specs_page_lists_it(specs, product, size, kept, dropped):
    chips = apple_options.chip_strings(_section(specs, product, size))
    assert [v for v in kept + dropped if apple_options.offered("chip", v, chips)] == kept
    assert apple_options.offered("memory", "64gb", chips), "only chips are filtered"


def test_the_air_15_drops_the_13_inch_chip_and_its_rules():
    fake = FakeApple()
    fake.update_config = fixture("apple_update_config_air13_1tb.json")  # 10-10 / 1TB, lists 10-8 too
    target = _air_target(section=_section("apple_macbook_air_specs.txt", "MacBook Air", 15))
    machine = apple_options_fetch.fetch_all([target], fake, pause_s=0)["machines"]["air13"]
    assert machine["chip_not_offered"] == ["10-8"]
    assert [o["value"] for o in machine["options"] if o["option_group"] == "chip"] == ["10-10"]
    assert not any("10-8" in (r["trigger_value"], r["forced_value"]) for r in machine["rules"])
    assert not any("uv.processor-cpuCoreCount-gpuCoreCount=10-8" in url for url in fake.calls),         "a chip the size does not have was asked about"


def test_a_chip_with_no_label_on_the_page_gets_one_from_the_specs_page():
    fake = FakeApple()
    fake.configure = fake.configure.replace('"10-10": {"dimensionComponents"', '"x": {"dimensionComponents"')
    machine = apple_options_fetch.fetch_all([_air_target()], fake, pause_s=0)["machines"]["air13"]
    chip = next(o for o in machine["options"] if o["value"] == "10-10")
    assert chip["label"] == "10-core CPU, 10-core GPU"
    assert machine["labels_missing"] == []


def _switch(add_on, *forced):
    return apple_options.Switch([apple_options.Rule("memory", "36gb", g, v) for g, v in forced], add_on)


def test_a_switched_option_is_priced_at_its_own_share():
    # Pro 14 M5 Pro 15c/16g / 24GB / 1TB, from the 2026-11-04 dry run.
    listed = [apple_options.Option("chip", "m5max-18-32", 3700.0, False, False),
              apple_options.Option("storage", "2tb", 2250.0, False, False),
              apple_options.Option("adapter", "96w", 80.0, False, False)]
    switches = {("memory", "36gb"): _switch(7300.0, ("chip", "m5max-18-32"), ("storage", "2tb"), ("adapter", "96w"))}
    [share] = apple_options.own_shares(listed, switches)
    assert (share.group, share.value, share.add_on_rm) == ("memory", "36gb", 1270.0)


def test_an_unpriced_forced_chip_adds_nothing():
    listed = [apple_options.Option("chip", "10-10", None, False, False)]
    [share] = apple_options.own_shares(listed, {("memory", "24gb"): _switch(900.0, ("chip", "10-10"))})
    assert share.add_on_rm == 900.0


def test_a_switch_forcing_another_switched_option_settles_in_rounds():
    switches = {("memory", "36gb"): _switch(7300.0, ("storage", "8tb")),
                ("storage", "8tb"): _switch(6000.0)}
    shares = {o.value: o.add_on_rm for o in apple_options.own_shares([], switches)}
    assert shares == {"36gb": 1300.0, "8tb": 6000.0}


def test_switches_that_only_force_each_other_are_refused():
    switches = {("memory", "36gb"): _switch(100.0, ("storage", "8tb")),
                ("storage", "8tb"): _switch(100.0, ("memory", "36gb"))}
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.own_shares([], switches)


def test_one_machines_failure_is_listed_and_the_rest_carry_on():
    fake = FakeApple()

    def get(url):
        if "broken" in url:
            return "<html>no bootstrap</html>"
        return fake(url)

    targets = [_air_target(laptop_id="bad", url=f"{AIR_BUY}/broken"), _air_target()]
    fetched = apple_options_fetch.fetch_all(targets, get, pause_s=0)
    assert [f["laptop_id"] for f in fetched["options_failed"]] == ["bad"]
    assert list(fetched["machines"]) == ["air13"]
    assert fetched["stopped"] is None


def test_a_block_stops_the_whole_step():
    fake = FakeApple()

    def get(url):
        if len(fake.calls) >= 8:
            raise apple_options_fetch.Blocked("HTTP 429")
        return fake(url)

    targets = [_air_target(), _air_target(laptop_id="second"), _air_target(laptop_id="third")]
    fetched = apple_options_fetch.fetch_all(targets, get, pause_s=0)
    assert list(fetched["machines"]) == ["air13"]
    assert fetched["stopped"]["laptop_id"] == "second" and "429" in fetched["stopped"]["reason"]
    assert fetched["options_failed"] == [], "a blocked machine is a stop, not a failure"
