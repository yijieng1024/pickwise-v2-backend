"""
Apple's configurator responses (step 10c, decided 2026-11-04), parsed into options and rules.

`cto/update-config` lists the options compatible with the kit that was sent, each with a
`priceDelta` relative to that kit, and marks the kit's own values `isSelected`. The kit
sent is the standard config, so a selected value is what it includes, and the add-on is
the delta minus the selected one's (zero when the kit was sent unchanged). Not
`isDefault`: that marks the collection's cheapest base (the 13-inch Air's 10-8 / 512GB)
on every standard config of the collection (seen in the 10c dry run, 2026-11-04).

An option the configure page shows but update-config does not list (24GB on the 10-8
Air, which needs the 10-core GPU chip) is reached through `cto/dependency-switcher`: it
answers "what happens if I pick X" with the kit and price before and after. Anything
that changed besides X itself is a forced change, i.e. a rule, and the price difference
is X's add-on.

The configure page (one standard config's buy page) gives the kit to send
(`warmStateBootstrap.preselectedDimensions`), the two endpoints, and the labels. The
specs page text gives the CPU and GPU strings of each chip option.

Pure: no database, no network. A response that is not the expected shape raises
`OptionsParseError`; the caller keeps the stored options and lists the machine in
`options_failed`.
"""
import html as html_lib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

# Apple's form field names differ by page: the Air says "processor-cpuCoreCount-gpuCoreCount",
# the Pro "processor-dimensionChip-cpuCoreCount-gpuCoreCount" (plus "processor-dimensionChip",
# the chip alone, which says nothing the core-count key does not). Colour and screen size
# are not options: on the buy page they pick a different standard config.
_PREFIX_GROUPS = {
    "memory": "memory",
    "storage": "storage",
    "power_adapter": "adapter",
    "keyboard": "keyboard",
    "display": "display",  # the Pro's standard / nano-texture finish
}
_CHIP_KEY = "processor-dimensionChip"
_DELTA = re.compile(r"-?\d+_\d+")
_CORES = re.compile(r"(?:([a-z0-9]+)-)?(\d+)-(\d+)")
# ponytail: only these can force a change as far as the trial showed (70W forced nothing);
# add a group here if a page proves otherwise.
_TRIGGER_GROUPS = ("chip", "memory", "storage")


class OptionsParseError(ValueError):
    """The response is not the shape the configurator returned in the 2026-11-04 trial."""


@dataclass(frozen=True)
class Option:
    group: str
    value: str
    add_on_rm: Optional[float]   # None: Apple prices no option of this group
    is_included: bool
    is_blocked: bool


@dataclass(frozen=True)
class Rule:
    trigger_group: str
    trigger_value: str
    forced_group: str
    forced_value: str


@dataclass(frozen=True)
class Switch:
    rules: list[Rule]
    add_on_rm: float   # the whole kit's price change, forced changes included


@dataclass(frozen=True)
class ConfigurePage:
    kit: dict[str, str]                 # Apple form field -> value, the standard config
    update_path: str                    # "/my/shop/api/cto/update-config?collection=...&fae=true"
    switcher_path: str
    labels: dict[tuple[str, str], str]  # (group, value) -> what the page shows


def group_of(key: str) -> Optional[str]:
    if key.endswith("cpuCoreCount-gpuCoreCount"):
        return "chip"
    return _PREFIX_GROUPS.get(key.split("-", 1)[0])


def cores_of(chip_value: str) -> Optional[tuple[int, int]]:
    """"10-8" and "m5pro-15-16" are (CPU cores, GPU cores)."""
    match = _CORES.fullmatch(chip_value)
    return (int(match.group(2)), int(match.group(3))) if match else None


def _delta(raw: Any) -> float:
    # "900_00" is RM 900.00, "-1350_00" is -RM 1,350.00, "0_0" is nothing.
    if not isinstance(raw, str) or not _DELTA.fullmatch(raw):
        raise OptionsParseError(f"unreadable priceDelta {raw!r}")
    return float(raw.replace("_", "."))


def parse_update_config(payload: Any) -> list[Option]:
    try:
        groups = payload["body"]["options"]
    except (KeyError, TypeError) as exc:
        raise OptionsParseError("no body.options in update-config response") from exc
    if not isinstance(groups, dict):
        raise OptionsParseError("body.options is not an object")

    options: list[Option] = []
    for key, entry in groups.items():
        group = group_of(key)
        if group is None:
            continue
        try:
            compatible = entry["compatibleOptions"]
            selected = [value for value, o in compatible.items() if o["isSelected"]]
            if len(selected) != 1:
                raise OptionsParseError(f"{key}: {len(selected)} selected options, expected 1")
            priced = [o.get("priceDelta") is not None for o in compatible.values()]
            if any(priced) and not all(priced):
                raise OptionsParseError(f"{key}: some options have a priceDelta and some do not")
            base = _delta(compatible[selected[0]]["priceDelta"]) if all(priced) else None
            for value, o in compatible.items():
                add_on = None if base is None else round(_delta(o["priceDelta"]) - base, 2)
                options.append(Option(group, value, add_on, value == selected[0], bool(o["isBlocked"])))
        except (KeyError, TypeError, AttributeError) as exc:
            raise OptionsParseError(f"{key}: unexpected shape ({exc!r})") from exc

    if not any(o.group in ("memory", "storage") for o in options):
        raise OptionsParseError("no memory or storage options in update-config response")
    return options


def _kit(overlay: dict, which: str) -> tuple[dict[str, str], float]:
    dimensions = overlay[which]["kitParts"]["dimensions"]
    price = overlay[which]["priceData"]["amount"]
    if not isinstance(price, (int, float)):
        raise OptionsParseError(f"{which} price {price!r} is not a number")
    return {key: d["dimensionValue"] for key, d in dimensions.items()}, float(price)


def parse_dependency_switcher(payload: Any, trigger_group: str, trigger_value: str) -> Switch:
    """The forced changes when `trigger_group` is set to `trigger_value` ([] when none),
    and what the switch costs."""
    try:
        overlay = payload["body"]["overlayContent"]
        (before, before_rm), (after, after_rm) = _kit(overlay, "currentKit"), _kit(overlay, "selectedKit")
    except (KeyError, TypeError) as exc:
        raise OptionsParseError("no currentKit/selectedKit in dependency-switcher response") from exc
    keys = [k for k in after if group_of(k) == trigger_group]
    if len(keys) != 1 or after[keys[0]] != trigger_value:
        found = {k: after[k] for k in keys}
        raise OptionsParseError(f"selected kit has {found}, not the requested {trigger_group}={trigger_value!r}")
    rules = [
        Rule(trigger_group, trigger_value, group, v)
        for k, v in after.items()
        if (group := group_of(k)) is not None and k != keys[0] and before.get(k) != v
    ]
    return Switch(rules, round(after_rm - before_rm, 2))


def triggers(options: list[Option]) -> list[Option]:
    """The listed options worth a dependency-switcher call: ones the standard config does
    not already have and can take."""
    return [o for o in options if o.group in _TRIGGER_GROUPS and not o.is_included and not o.is_blocked]


def unlisted(options: list[Option], page: "ConfigurePage") -> list[tuple[str, str]]:
    """(group, value) the configure page shows but update-config did not list: reachable
    only through a forced change, so priced by the dependency-switcher."""
    listed = {(o.group, o.value) for o in options}
    return [key for key in page.labels if key[0] in _TRIGGER_GROUPS and key not in listed]


# --------------------------------------------------------------------------- configure page

def _json_after(page: str, marker: str) -> Any:
    start = page.find(marker)
    if start < 0:
        raise OptionsParseError(f"no {marker.strip(' =:')!r} on the configure page")
    try:
        return json.JSONDecoder().raw_decode(page, start + len(marker))[0]
    except json.JSONDecodeError as exc:
        raise OptionsParseError(f"{marker.strip(' =:')!r} is not JSON") from exc


def _endpoint(page: str, name: str) -> str:
    match = re.search(rf"{name}: '(/my/shop/api/cto/[^']+)'", page)
    if not match:
        raise OptionsParseError(f"no {name} on the configure page")
    return html_lib.unescape(match.group(1))


def _clean(header: str) -> str:
    text = re.sub(r"<[^>]+>", " ", header.split("<div", 1)[0])
    text = html_lib.unescape(text).replace("‑", "-").replace("\xa0", " ")
    return " ".join(text.split()).strip(" ,")


def parse_configure_page(page: str) -> ConfigurePage:
    warm = _json_after(page, "warmStateBootstrap = ")
    kit = warm.get("preselectedDimensions") if isinstance(warm, dict) else None
    if not isinstance(kit, dict) or not any(group_of(k) in ("memory", "storage") for k in kit):
        raise OptionsParseError("no preselected memory or storage on the configure page")
    selection = _json_after(page, "productSelectionData: ")
    labels: dict[tuple[str, str], str] = {}
    try:
        shown = {**selection.get("mainDisplayValues", {}), **selection.get("configDisplayValues", {})}
    except AttributeError as exc:
        raise OptionsParseError("productSelectionData is not an object") from exc
    for key, values in shown.items():
        group = group_of(key)
        if group is None or not isinstance(values, dict):
            continue
        for value, display in values.items():
            if isinstance(display, dict) and isinstance(display.get("header"), str) and _clean(display["header"]):
                labels[(group, value)] = _clean(display["header"])
    return ConfigurePage({str(k): str(v) for k, v in kit.items()},
                         _endpoint(page, "updateConfigUrl"), _endpoint(page, "ctoDependencySwitcherUrl"), labels)


def query(kit: Mapping[str, str], group: Optional[str] = None, value: Optional[str] = None) -> str:
    """The kit as `sv.` parameters, plus the change as `uv.`, the way the page sends them.
    A chip change also names the chip alone when the kit has that field."""
    params = [(f"sv.{k}", v) for k, v in kit.items()]
    if group is not None:
        keys = [k for k in kit if group_of(k) == group]
        if len(keys) != 1:
            raise OptionsParseError(f"the kit has {len(keys)} fields for {group}")
        params.append((f"uv.{keys[0]}", value))
        prefix = _CORES.fullmatch(value or "")
        if group == "chip" and _CHIP_KEY in kit and prefix and prefix.group(1):
            params.append((f"uv.{_CHIP_KEY}", prefix.group(1)))
    return urlencode(params)


# --------------------------------------------------------------------------- specs page

_CHIP_LINE = re.compile(r"Apple ([AM]\d+(?: (?:Pro|Max|Ultra))?) chip")
_CONFIGURABLE = re.compile(r"([AM]\d+(?: (?:Pro|Max|Ultra))?) with (\d+)-core CPU and (\d+)-core GPU")


def chip_strings(section: Optional[str]) -> dict[tuple[int, int], tuple[str, str]]:
    """(CPU cores, GPU cores) -> (processor_model, gpu_model) in the catalog's form, for
    every chip the size section names: its chip blocks and "Configurable to" lines. A
    core pair two chips share is left out rather than guessed."""
    found: dict[tuple[int, int], set[str]] = defaultdict(set)
    chip: Optional[str] = None
    cpu: Optional[int] = None
    text = (section or "").replace("‑", "-").replace("\xa0", " ")
    for line in (line.strip() for line in text.splitlines()):
        if match := _CHIP_LINE.fullmatch(line):
            chip, cpu = match.group(1), None
        elif match := _CONFIGURABLE.search(line):
            found[(int(match.group(2)), int(match.group(3)))].add(match.group(1))
        elif chip and cpu is None and (match := re.match(r"(\d+)-core CPU", line)):
            cpu = int(match.group(1))
        elif chip and cpu is not None and (match := re.match(r"(\d+)-core GPU", line)):
            found[(cpu, int(match.group(1)))].add(chip)
            chip = None
    return {cores: (f"Apple {next(iter(chips))} ({cores[0]}-core)", f"{cores[1]}-core GPU")
            for cores, chips in found.items() if len(chips) == 1}
