"""Apple configurator responses read as chip kits (step 10c). Fixtures are real responses
from 2026-11-04 on the MacBook Air 13 (M5): its 10-8 / 16GB / 512GB base (RM 5,499) and
its 10-10 / 16GB / 1TB standard config (RM 6,849), each sent as itself; prices in the
quote tests are the real ones Apple gave. No database, no network."""
import copy
import json
from pathlib import Path

import pytest

from tests._apple_fake import AIR13_1010_SLUG, AIR13_SLUG, AIR_BUY, FakeApple, fixture

from ._adapters import apple_fields, apple_name, apple_options, apple_options_fetch

_DIR = Path(__file__).parent / "fixtures" / "scraper"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text(encoding="utf-8"))


# The 10-8 base: update-config lists only 16GB and 512GB, which is all that chip allows.
BASE = _load("apple_update_config_air13_standard.json")
# The 10-10 / 1TB standard config: Apple marks the base's 512GB `isDefault`, its own 1TB
# `isSelected`, and prices relative to 1TB (512GB is -1,350).
ONE_TB = _load("apple_update_config_air13_1tb.json")
SWITCH_24GB = _load("apple_dependency_switcher_air13_24gb.json")
AIR_PAGE = fixture("apple_configure_air13.html")
PRO_PAGE = fixture("apple_configure_pro14_proled.html")


def _add_ons(kit, group):
    return {o.value: o.add_on_rm for o in kit.options if o.group == group}


# --------------------------------------------------------------------------- one kit

def test_a_kit_is_priced_relative_to_itself_not_apples_default():
    kit = apple_options.parse_kit(ONE_TB)
    assert (kit.chip, kit.price_rm) == ("10-10", 6849.0)
    assert _add_ons(kit, "memory") == {"16gb": 0.0, "24gb": 900.0, "32gb": 1800.0}
    assert _add_ons(kit, "storage") == {"512gb": -1350.0, "1tb": 0.0, "2tb": 2250.0, "4tb": 6750.0}
    included = {(o.group, o.value) for o in kit.options if o.is_included}
    assert included == {("memory", "16gb"), ("storage", "1tb"), ("adapter", "40w"), ("keyboard", "none")}


def test_a_kit_lists_only_what_its_chip_allows():
    kit = apple_options.parse_kit(BASE)
    assert (kit.chip, kit.price_rm) == ("10-8", 5499.0)
    assert _add_ons(kit, "memory") == {"16gb": 0.0} and _add_ons(kit, "storage") == {"512gb": 0.0}
    assert _add_ons(kit, "adapter") == {"40w": 0.0, "35w": 50.0, "70w": 50.0}
    assert set(_add_ons(kit, "keyboard").values()) == {None}


def test_blocked_options_are_flagged_and_not_allowed():
    # No real response had a blocked option, so one is marked by hand.
    payload = copy.deepcopy(ONE_TB)
    payload["body"]["options"]["storage-dimensionCapacity"]["compatibleOptions"]["4tb"]["isBlocked"] = True
    kit = apple_options.parse_kit(payload)
    assert [o.value for o in kit.options if o.is_blocked] == ["4tb"]
    assert not kit.allows("storage", "4tb") and kit.allows("storage", "2tb")


@pytest.mark.parametrize("payload", [
    None,
    "<html>Access Denied</html>",
    {"head": {"status": "200"}, "body": {}},
    {"body": {"options": {}, "selectedKits": {"priceData": {"amount": 5499.0}}}},
])
def test_malformed_update_config_fails_cleanly(payload):
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_kit(payload)


def test_unreadable_or_missing_prices_fail_cleanly():
    payload = copy.deepcopy(ONE_TB)
    payload["body"]["options"]["memory-dimensionMemory"]["compatibleOptions"]["24gb"]["priceDelta"] = "RM 900"
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_kit(payload)
    payload = copy.deepcopy(ONE_TB)
    payload["body"]["selectedKits"]["priceData"]["amount"] = "RM 6,849"
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.parse_kit(payload)


def test_the_switcher_gives_apples_own_kit_for_a_chip():
    kit = apple_options.switched_kit(SWITCH_24GB, "10-10")
    assert kit["processor-cpuCoreCount-gpuCoreCount"] == "10-10" and kit["memory-dimensionMemory"] == "24gb"
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.switched_kit(SWITCH_24GB, "10-8")
    with pytest.raises(apple_options.OptionsParseError):
        apple_options.switched_kit({"body": {}}, "10-10")


# --------------------------------------------------------------------------- quote

AIR13 = {"10-8": apple_options.parse_kit(BASE), "10-10": apple_options.parse_kit(ONE_TB)}
AIR13_BASE_CHOICES = {"memory": "16gb", "storage": "512gb", "adapter": "40w", "keyboard": "none"}


def test_a_total_is_the_kit_price_plus_its_add_ons():
    q = apple_options.quote(AIR13, "10-10", {**AIR13_BASE_CHOICES, "memory": "24gb", "storage": "2tb"})
    assert (q.chip, q.total_rm, q.chip_changed) == ("10-10", 6849.0 + 900 + 2250, False)


def test_an_option_only_another_chip_allows_is_a_chip_change():
    # 24GB on the 10-8 base: Apple's own switcher answer is the 10-10 at RM 6,399.
    q = apple_options.quote(AIR13, "10-8", {**AIR13_BASE_CHOICES, "memory": "24gb"})
    assert (q.chip, q.total_rm, q.chip_changed) == ("10-10", 6399.0, True)


def test_a_choice_no_chip_allows_is_refused():
    with pytest.raises(ValueError):
        apple_options.quote(AIR13, "10-8", {**AIR13_BASE_CHOICES, "memory": "128gb"})


def _pro_kit(price, memory, storage):
    options = [apple_options.Option("memory", v, a, a == 0, False) for v, a in memory.items()]
    options += [apple_options.Option("storage", v, a, a == 0, False) for v, a in storage.items()]
    return {"m5pro-15-16": apple_options.Kit("m5pro-15-16", price, options)}


def test_the_two_pro_lines_price_the_same_chip_separately():
    # Pro 14 M5 Pro 15c/16g 24GB / 1TB kits, 2026-11-04: Liquid Retina RM 10,299, PROLED RM 11,999.
    memory, storage = {"24gb": 0.0, "48gb": 2700.0}, {"1tb": 0.0, "2tb": 2250.0, "4tb": 6750.0}
    choices = {"memory": "48gb", "storage": "2tb"}
    liquid = apple_options.quote(_pro_kit(10299.0, memory, storage), "m5pro-15-16", choices)
    assert liquid.total_rm == 15249.0, "Apple's own Liquid Retina total"
    proled = apple_options.quote(_pro_kit(11999.0, memory, storage), "m5pro-15-16", choices)
    assert proled.total_rm == 16949.0


# --------------------------------------------------------------------------- pages

def test_configure_page_gives_the_kit_the_endpoints_and_the_labels():
    air = apple_options.parse_configure_page(AIR_PAGE)
    assert air.chip == "10-8" and air.kit["memory-dimensionMemory"] == "16gb"
    assert air.update_path == "/my/shop/api/cto/update-config?collection=MBA_2026_COLLECTION&fae=true"
    assert air.labels[("memory", "24gb")] == "24GB"
    assert air.labels[("chip", "10-10")] == "10-core CPU, 10-core GPU, 16-core Neural Engine"
    assert air.labels[("adapter", "40w")] == "40W Dynamic Power Adapter with 60W Max"
    pro = apple_options.parse_configure_page(PRO_PAGE)
    assert pro.chip == "m5pro-15-16"
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


def _section(specs: str, product: str, size: int):
    return apple_fields.section_for(apple_name.size_sections(fixture(specs), product), size)


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
    assert [v for v in kept + dropped if apple_options.offered(v, chips)] == kept


# --------------------------------------------------------------------------- fetch

def _air(size=13, slugs=(AIR13_SLUG, AIR13_1010_SLUG), line=""):
    cores = {AIR13_SLUG: (10, 8), AIR13_1010_SLUG: (10, 10)}
    return {"product": "MacBook Air", "size_inch": size, "line": line,
            "section": _section("apple_macbook_air_specs.txt", "MacBook Air", size),
            "standards": [{"cores": cores[s], "url": f"{AIR_BUY}/{s}"} for s in slugs]}


def _kits(fetched):
    return {k["chip"]: k for k in fetched["kits"]}


def _rows(kit):
    return {(r["option_group"], r["value"]): r for r in kit["rows"]}


def test_each_chip_of_a_size_is_one_kit_from_one_update_config_call():
    fake = FakeApple()
    fetched = apple_options_fetch.fetch_all([_air()], fake, pause_s=0)
    kits = _kits(fetched)
    assert set(kits) == {"10-8", "10-10"}
    chip = _rows(kits["10-10"])[("chip", "10-10")]
    assert (chip["kit_price_rm"], chip["cpu_model"], chip["gpu_model"]) == (6849.0, "Apple M5 (10-core)", "10-core GPU")
    assert _rows(kits["10-10"])[("storage", "512gb")]["add_on_rm"] == -1350.0
    assert set(_rows(kits["10-8"])) >= {("memory", "16gb"), ("storage", "512gb")}
    assert ("memory", "24gb") not in _rows(kits["10-8"]), "an option of another chip landed on this kit"
    assert fetched["calls"] == 4 == len(fake.calls), "2 configure pages + 2 update-config"
    assert fetched["options_failed"] == [] and fetched["labels_missing"] == []


def test_a_chip_with_no_standard_config_gets_apples_own_kit_from_the_switcher():
    fake = FakeApple()
    fetched = apple_options_fetch.fetch_all([_air(slugs=(AIR13_SLUG,))], fake, pause_s=0)
    assert set(_kits(fetched)) == {"10-8", "10-10"}
    assert sum("/cto/dependency-switcher" in url for url in fake.calls) == 1
    assert fetched["calls"] == 4, "1 configure page + 1 switcher + 2 update-config"


def test_the_air_15_drops_the_13_inch_chip_without_asking_about_it():
    fake = FakeApple()
    fetched = apple_options_fetch.fetch_all([_air(size=15, slugs=(AIR13_1010_SLUG,))], fake, pause_s=0)
    assert set(_kits(fetched)) == {"10-10"}
    assert fetched["chip_not_offered"] == [{"product": "MacBook Air", "size_inch": 15, "line": "", "chips": ["10-8"]}]
    assert not any("10-8" in url and "/cto/" in url for url in fake.calls), "a chip the size does not have was asked about"


def test_a_kit_carries_its_line():
    fetched = apple_options_fetch.fetch_all([_air(line="liquid-retina")], FakeApple(), pause_s=0)
    assert {k["line"] for k in fetched["kits"]} == {"liquid-retina"}


def test_a_chip_with_no_label_on_the_page_gets_one_from_the_specs_page():
    fake = FakeApple()
    fake.configure = fake.configure.replace('"10-10": {"dimensionComponents"', '"x": {"dimensionComponents"')
    kits = _kits(apple_options_fetch.fetch_all([_air()], fake, pause_s=0))
    assert _rows(kits["10-10"])[("chip", "10-10")]["label"] == "10-core CPU, 10-core GPU"


def test_one_kits_failure_is_listed_and_the_rest_carry_on():
    fake = FakeApple()
    fake.update_config["10-10"] = "<html>no</html>"
    fetched = apple_options_fetch.fetch_all([_air()], fake, pause_s=0)
    assert set(_kits(fetched)) == {"10-8"}
    assert [(f["size_inch"], f["chip"]) for f in fetched["options_failed"]] == [(13, "10-10")]


def test_a_size_whose_configure_page_fails_is_listed():
    fake = FakeApple()
    fake.configure = "<html>no bootstrap</html>"
    fetched = apple_options_fetch.fetch_all([_air()], fake, pause_s=0)
    assert fetched["kits"] == [] and fetched["options_failed"][0]["chip"] is None


def test_a_block_stops_the_whole_step():
    fake = FakeApple()

    def get(url):
        if len(fake.calls) >= 4:
            raise apple_options_fetch.Blocked("HTTP 429")
        return fake(url)

    fetched = apple_options_fetch.fetch_all([_air(), _air(size=15, slugs=(AIR13_1010_SLUG,))], get, pause_s=0)
    assert {k["size_inch"] for k in fetched["kits"]} == {13}
    assert fetched["stopped"]["size_inch"] == 15 and "429" in fetched["stopped"]["reason"]
    assert fetched["options_failed"] == [], "a block is a stop, not a failure"
