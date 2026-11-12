"""get_configuration_options (Pico's configurator, 10c step e) on the Pro chip kits stored
in production on 2026-11-04. No database: configurable() is replaced by a Configurable
built from the fixture, the scoring data by the configure test's."""
import json
import uuid
from pathlib import Path

import pytest

from ._adapters import config_options_tool as tool_mod
from ._adapters import laptop_models, options_service as svc
from .test_apple_configure import SCORING

ROWS = json.loads((Path(__file__).parent / "fixtures" / "apple_kits_pro_2026-11-04.json").read_text(encoding="utf-8"))
EVAL_LIMIT = 4500  # eval/run_eval.py's per-output truncation

# (size, line, standard chip, ram, ssd, price): a real standard config of each Pro size and line
STANDARD = {
    (14, "liquid-retina"): ("m6-12-12", 16, 1024, 8299.0),
    (14, "proled"): ("m5pro-15-16", 24, 1024, 11999.0),
    (16, "liquid-retina"): ("m5pro-18-20", 24, 1024, 15299.0),
    (16, "proled"): ("m5max-18-40", 48, 2048, 22999.0),
}


def _configurable(size, line):
    chip, ram, ssd, price = STANDARD[(size, line)]
    rows = [r for r in ROWS if r["size_inch"] == size and r["line"] == line]
    laptop = laptop_models.Laptop(id=uuid.uuid4(), brand_id=uuid.uuid4(), product_name=f"MacBook Pro {size}",
                                  model_code=f"apple-macbook-pro-{size}-{line}", price_rm=price, ram_gb=ram,
                                  ssd_gb=ssd, display_size_inch=size + 0.2, weight_kg=1.6, battery_wh=72.4)
    kits = svc.apple_options.kits_from_rows(rows)
    standard = {"memory": svc._value(ram), "storage": svc._value(ssd)}
    for group in svc.GROUPS[2:]:
        if (v := svc._included(kits[chip], group)) is not None:
            standard[group] = v
    return svc.Configurable(laptop, rows, kits, chip, standard, laptop.price_rm)


@pytest.fixture
def pro(monkeypatch):
    def use(size, line):
        c = _configurable(size, line)
        monkeypatch.setattr(svc, "configurable", lambda session, laptop_id: c)
        monkeypatch.setattr(svc, "get_scoring_data", lambda session: SCORING)
        return c
    return use


def _call(c, selections=None):
    return tool_mod.configuration_options(None, str(c.laptop.id), selections)


@pytest.mark.parametrize("size, line", list(STANDARD))
def test_the_listing_fits_the_eval_truncation(pro, size, line):
    out = _call(pro(size, line))
    size_chars = len(json.dumps(out, ensure_ascii=False))
    assert size_chars < EVAL_LIMIT, size_chars
    assert "keyboard" not in json.dumps(out)


def test_the_listing_is_the_options_endpoint_s_kits(pro):
    c = pro(14, "liquid-retina")
    out = _call(c)
    assert out["standard"] == {"chip": "m6-12-12", "memory": "16gb", "storage": "1tb", **{
        g: v for g, v in c.standard.items() if g in ("adapter", "display")}}
    m6 = next(ch for ch in out["chips"] if ch["chip"] == "m6-12-12")
    assert m6["add_on_rm"] == 0.0 and m6["memory"]["16gb"] == 0.0


def test_every_listed_price_is_the_configure_total_minus_the_standard_price(pro):
    c = pro(14, "liquid-retina")
    for ch in _call(c)["chips"]:
        mem = next(v for v in ch["memory"] if v != "16gb")
        out = _call(c, {"chip": ch["chip"], "memory": mem})
        assert out["total_rm"] == round(c.laptop.price_rm + ch["memory"][mem], 2), ch["chip"]
    assert _call(c, {"memory": "32gb", "storage": "4tb"})["total_rm"] == 16849.0  # Apple's own


def test_selections_total_apples_own_figure_with_a_live_score(pro):
    out = _call(pro(14, "liquid-retina"), {"chip": "m5pro-15-16", "RAM": "48 GB", "ssd": "2TB"})
    assert out["total_rm"] == 15249.0, "Apple's own total for Pro 14 Liquid Retina M5 Pro + 48GB + 2TB"
    assert out["configured"] is True and out["add_ons"]["memory"]["value"] == "48gb"
    assert set(out["live_pick_scores"]) == {"office_study", "programming", "gaming", "creative_work", "general_use"}
    assert "not a catalog laptop" in out["note"]


def test_a_choice_another_chip_offers_names_that_chip_and_does_not_switch(pro):
    out = _call(pro(16, "proled"), {"memory": "36gb"})  # only the 32-core GPU M5 Max has 36GB
    assert "total_rm" not in out
    [problem] = out["not_allowed"]
    assert problem["value"] == "36gb" and out["chip"] == "m5max-18-40"
    assert [o["chip"] for o in problem["offered_by"]] == ["m5max-18-32"]
    assert problem["offered_by"][0]["label"] == "18-core CPU, 32-core GPU"
    offered_total = _call(pro(16, "proled"), {"chip": "m5max-18-32", "memory": "36gb"})["total_rm"]
    assert offered_total == 22999.0 + problem["offered_by"][0]["add_on_rm"]


# --------------------------------------------------------------------------- the configuration's path
# Plan step 10d+ ("The URL rule"). The cases are shared with the frontend's buildConfigPath:
# the same file is its src/lib/config-url.cases.json, and its test fails when the two differ.

SHARED = json.loads((Path(__file__).parent / "fixtures" / "config_url_cases.json").read_text(encoding="utf-8"))


def _path(c, selections):
    return _call(c, selections)["path"].replace(str(c.laptop.id), "{id}")


@pytest.mark.parametrize("case", SHARED["cases"], ids=lambda case: case["name"])
def test_a_configurations_path_is_the_shared_cases(pro, case):
    assert _path(pro(14, "liquid-retina"), case["picks"]) == case["path"]


def test_the_shared_cases_options_are_this_laptops_own():
    """The frontend builds its paths from `options`; it has to be what GET /options gives."""
    c = _configurable(14, "liquid-retina")
    options = SHARED["options"]
    assert (options["standard_chip"], options["standard"], options["standard_price_rm"]) == (
        c.chip, c.standard, c.standard_price_rm)
    assert options["kits"] == json.loads(json.dumps(svc.kits_listing(c)))


def test_picks_that_are_the_standard_configs_own_are_the_plain_path(pro):
    c = pro(14, "liquid-retina")
    assert _path(c, {"memory": "16gb"}) == "/laptops/{id}"
    assert _path(c, {"chip": "m6-12-12", "storage": "1tb", "adapter": "70w", "display": "standard"}) == "/laptops/{id}"
    # One pick away from the standard config: params, and all three base ones.
    assert _path(c, {"storage": "2tb"}) == "/laptops/{id}?chip=m6-12-12&memory=16gb&storage=2tb"
    assert _path(c, {"adapter": "96w"}) == "/laptops/{id}?chip=m6-12-12&memory=16gb&storage=1tb&adapter=96w"
    assert _path(c, {"chip": "m5pro-15-16"}) == "/laptops/{id}?chip=m5pro-15-16&memory=24gb&storage=1tb"


def test_adapter_and_display_are_left_out_when_they_are_what_the_kit_comes_with(pro):
    c = pro(14, "liquid-retina")
    # The M5 Pro 18c/20g kit comes with 96W, so 96W on that chip is not a choice to write.
    assert _path(c, {"chip": "m5pro-18-20"}) == "/laptops/{id}?chip=m5pro-18-20&memory=24gb&storage=1tb"
    assert _path(c, {"chip": "m5pro-18-20", "adapter": "96w"}) == _path(c, {"chip": "m5pro-18-20"})
    assert _path(c, {"memory": "32gb", "adapter": "70w", "display": "standard"}) == \
        "/laptops/{id}?chip=m6-12-12&memory=32gb&storage=1tb"


def test_the_param_order_is_fixed_whatever_order_the_selections_came_in(pro):
    c = pro(14, "liquid-retina")
    expected = "/laptops/{id}?chip=m6-12-12&memory=32gb&storage=1tb&adapter=96w&display=nano_texture"
    assert _path(c, {"display": "nano-texture glass", "adapter": "96w", "memory": "32gb"}) == expected
    assert _path(c, {"memory": "32gb", "chip": "m6-12-12", "adapter": "96w", "display": "nano_texture"}) == expected
    assert "keyboard" not in expected


def test_a_paths_params_are_configure_s_own_and_give_the_same_total(pro):
    from urllib.parse import parse_qsl, urlsplit

    c = pro(14, "liquid-retina")
    out = _call(c, {"chip": "m5pro-15-16", "memory": "48gb", "storage": "2tb", "adapter": "96w"})
    params = dict(parse_qsl(urlsplit(out["path"]).query))
    assert list(params) == ["chip", "memory", "storage", "adapter"]
    assert _call(c, params)["total_rm"] == out["total_rm"] == 15249.0 + 80.0


def test_only_a_configuration_has_a_path(pro, monkeypatch):
    c = pro(16, "proled")
    assert "path" not in _call(c), "the listing is not a configuration"
    assert "path" not in _call(c, {"memory": "36gb"}), "a choice the chip does not allow"

    def none(session, laptop_id):
        raise svc.NotConfigurable
    monkeypatch.setattr(svc, "configurable", none)
    monkeypatch.setattr(tool_mod, "_configurable_siblings", lambda session, laptop_id: [])
    assert "path" not in tool_mod.configuration_options(None, str(uuid.uuid4())), "a laptop with no options"


def test_a_laptop_without_options_says_so(monkeypatch):
    def none(session, laptop_id):
        raise svc.NotConfigurable
    monkeypatch.setattr(svc, "configurable", none)
    monkeypatch.setattr(tool_mod, "_configurable_siblings", lambda session, laptop_id: [])
    assert "no build-to-order options" in tool_mod.configuration_options(None, str(uuid.uuid4()))["error"]
    assert "not a laptop id" in tool_mod.configuration_options(None, "MacBook Air")["error"]


def test_pico_has_the_configurator_and_not_the_customizations_tool():
    from ._adapters import agent_graph
    from ._adapters import agent_tools as ALL_TOOLS
    names = [t.name for t in ALL_TOOLS]
    assert "get_configuration_options" in names and "calculate_custom_apple_price" not in names
    prompt = agent_graph._SYSTEM_PROMPT
    assert f"access to {['one', 'two', 'three', 'four', 'five', 'six'][len(names) - 1]} tools" in prompt
    assert all(f"{i}. {n}" in prompt for i, n in enumerate(sorted(names, key=prompt.index), 1))
    assert "calculate_custom_apple_price" not in prompt and "not a catalog ranking" in prompt


def test_pico_is_told_to_link_a_configuration_with_the_tools_path_and_never_its_own():
    from ._adapters import agent_graph
    prompt = " ".join(agent_graph._SYSTEM_PROMPT.split())
    assert "get_configuration_options returns a path" in prompt
    assert "markdown link with exactly that path" in prompt
    assert "Never build, edit or guess a URL" in prompt and "returned no path, give no link" in prompt


def test_a_build_to_order_row_points_at_its_family_s_standard_configs(monkeypatch):
    def none(session, laptop_id):
        raise svc.NotConfigurable
    std = [{"laptop_id": "x", "product_name": "MacBook Pro 14 (M5 Pro)", "price_rm": 10299.0}]
    monkeypatch.setattr(svc, "configurable", none)
    monkeypatch.setattr(tool_mod, "_configurable_siblings", lambda session, laptop_id: std)
    out = tool_mod.configuration_options(None, str(uuid.uuid4()))
    assert out["configurable_standard_configs"] == std and "standard configs do" in out["error"]
