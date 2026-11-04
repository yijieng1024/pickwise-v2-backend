"""
Apple's configurator, read as chip kits (step 10c, decided 2026-11-04).

Apple's options are chip-driven: the chip decides which RAM and storage exist; the size
only decides which chips exist. A chip kit is one chip on one product, size and line,
with Apple's price for a valid kit of it and the options it allows.

`cto/update-config`, sent a valid kit, lists exactly what that kit's chip allows, each
with a `priceDelta` relative to the kit, marks the kit's own values `isSelected`, and
prices the kit (`selectedKits.priceData`). One call is one chip kit. (`isDefault` marks
the collection's cheapest base on every kit, so it is never read.)

A valid kit for a chip comes from a standard config's configure page when one has that
chip, else from `cto/dependency-switcher`: asked for that chip, its `selectedKit` is
Apple's own kit for it (RAM and storage snapped to what the chip allows). The snapping is
not a rule between options: reading it as one is what made the first model's dry run see
bundles that do not exist.

A configuration's price is its kit's price plus its options' add-ons on that kit
(`quote`); a choice its chip does not allow is a chip change.

Pure: no database, no network. A response that is not the expected shape raises
`OptionsParseError`; the caller keeps the stored kit and lists it in `options_failed`.
"""
import html as html_lib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
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


class OptionsParseError(ValueError):
    """The response is not the shape the configurator returned on 2026-11-04."""


@dataclass(frozen=True)
class Option:
    group: str
    value: str
    add_on_rm: Optional[float]   # relative to the kit; None: Apple prices no option of this group
    is_included: bool
    is_blocked: bool


@dataclass(frozen=True)
class Kit:
    chip: str
    price_rm: float
    options: list[Option] = field(default_factory=list)   # every group but the chip

    def allows(self, group: str, value: str) -> bool:
        return any(o.group == group and o.value == value and not o.is_blocked for o in self.options)

    def add_on(self, group: str, value: str) -> float:
        return next(o.add_on_rm or 0.0 for o in self.options if o.group == group and o.value == value)


@dataclass(frozen=True)
class Quote:
    chip: str
    total_rm: float
    chip_changed: bool


@dataclass(frozen=True)
class ConfigurePage:
    kit: dict[str, str]                 # Apple form field -> value, the standard config
    update_path: str                    # "/my/shop/api/cto/update-config?collection=...&fae=true"
    switcher_path: str
    labels: dict[tuple[str, str], str]  # (group, value) -> what the page shows

    @property
    def chip(self) -> str:
        return chip_of_kit(self.kit)


def group_of(key: str) -> Optional[str]:
    if key.endswith("cpuCoreCount-gpuCoreCount"):
        return "chip"
    return _PREFIX_GROUPS.get(key.split("-", 1)[0])


def cores_of(chip_value: str) -> Optional[tuple[int, int]]:
    """"10-8" and "m5pro-15-16" are (CPU cores, GPU cores)."""
    match = _CORES.fullmatch(chip_value)
    return (int(match.group(2)), int(match.group(3))) if match else None


def chip_of_kit(kit: Mapping[str, str]) -> str:
    chips = [v for k, v in kit.items() if group_of(k) == "chip"]
    if len(chips) != 1:
        raise OptionsParseError(f"the kit has {len(chips)} chip fields")
    return chips[0]


def _delta(raw: Any) -> float:
    # "900_00" is RM 900.00, "-1350_00" is -RM 1,350.00, "0_0" is nothing.
    if not isinstance(raw, str) or not _DELTA.fullmatch(raw):
        raise OptionsParseError(f"unreadable priceDelta {raw!r}")
    return float(raw.replace("_", "."))


def parse_kit(payload: Any) -> Kit:
    """One update-config response, sent a valid kit, as that kit."""
    try:
        groups = payload["body"]["options"]
        price = payload["body"]["selectedKits"]["priceData"]["amount"]
    except (KeyError, TypeError) as exc:
        raise OptionsParseError("no body.options or kit price in update-config response") from exc
    if not isinstance(groups, dict) or not isinstance(price, (int, float)):
        raise OptionsParseError("body.options is not an object, or the kit price is not a number")

    chip: Optional[str] = None
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
            if group == "chip":
                chip = selected[0]
                continue
            priced = [o.get("priceDelta") is not None for o in compatible.values()]
            if any(priced) and not all(priced):
                raise OptionsParseError(f"{key}: some options have a priceDelta and some do not")
            base = _delta(compatible[selected[0]]["priceDelta"]) if all(priced) else None
            for value, o in compatible.items():
                add_on = None if base is None else round(_delta(o["priceDelta"]) - base, 2)
                options.append(Option(group, value, add_on, value == selected[0], bool(o["isBlocked"])))
        except (KeyError, TypeError, AttributeError) as exc:
            raise OptionsParseError(f"{key}: unexpected shape ({exc!r})") from exc

    if chip is None or not any(o.group in ("memory", "storage") for o in options):
        raise OptionsParseError("no chip, memory or storage in update-config response")
    return Kit(chip, float(price), options)


def switched_kit(payload: Any, chip: str) -> dict[str, str]:
    """Apple's own kit for `chip`, from a dependency-switcher answer asking for it."""
    try:
        dimensions = payload["body"]["overlayContent"]["selectedKit"]["kitParts"]["dimensions"]
        kit = {key: d["dimensionValue"] for key, d in dimensions.items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise OptionsParseError("no selectedKit in dependency-switcher response") from exc
    if chip_of_kit(kit) != chip:
        raise OptionsParseError(f"selected kit has chip {chip_of_kit(kit)!r}, not the requested {chip!r}")
    return kit


def quote(kits: Mapping[str, Kit], chip: str, choices: Mapping[str, str]) -> Quote:
    """The price of `choices` (group -> value, every group the configuration has, not only
    the changed ones) on `chip`. A choice `chip` does not allow is a chip change: to the
    cheapest kit that allows every choice. Raises when none does."""
    def allows(kit: Kit) -> bool:
        return all(kit.allows(group, value) for group, value in choices.items())

    kit = kits[chip] if allows(kits[chip]) else min(
        (k for k in kits.values() if allows(k)), key=lambda k: k.price_rm, default=None)
    if kit is None:
        raise ValueError(f"no chip allows {dict(choices)}")
    total = kit.price_rm + sum(kit.add_on(group, value) for group, value in choices.items())
    return Quote(kit.chip, round(total, 2), kit.chip != chip)


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
    configure = ConfigurePage({str(k): str(v) for k, v in kit.items()},
                              _endpoint(page, "updateConfigUrl"), _endpoint(page, "ctoDependencySwitcherUrl"), labels)
    configure.chip  # noqa: B018 -- raises when the kit has no single chip
    return configure


def query(kit: Mapping[str, str], group: Optional[str] = None, value: Optional[str] = None) -> str:
    """The kit as `sv.` parameters, plus a change as `uv.`, the way the page sends them.
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


def offered(chip: str, chips: Mapping[tuple[int, int], Any]) -> bool:
    """Apple's API lists every chip of the collection on every size (the Air 15 gets the
    13-inch-only 10-core / 8-core GPU chip). A chip counts only if the size's specs page
    lists it (decided 2026-11-04); `chips` is `chip_strings` of that section."""
    return cores_of(chip) in chips
