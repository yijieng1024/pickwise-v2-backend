"""
Apple's configurator responses (step 10c, decided 2026-11-04), parsed into options and rules.

`cto/update-config` lists every option of a standard config with a `priceDelta` relative
to the kit that was sent, and flags the standard config's own value as `isDefault`. The
add-on stored is relative to the standard config, so each delta has the default's delta
subtracted: the same numbers come out whichever kit the call was made from.

`cto/dependency-switcher` answers "what happens if I pick X" with the kit before and
after. Anything that changed besides X itself is a forced change, i.e. a rule.

Pure: no database, no network. A response that is not the expected shape raises
`OptionsParseError`; the caller keeps the stored options and lists the machine in
`options_failed`.
"""
import re
from dataclasses import dataclass
from typing import Any, Optional

# Apple's form field name -> our group. Colour and screen size are not options: on the
# buy page they pick a different standard config.
GROUPS = {
    "processor-cpuCoreCount-gpuCoreCount": "chip",
    "memory-dimensionMemory": "memory",
    "storage-dimensionCapacity": "storage",
    "power_adapter-wattage": "adapter",
    "keyboard-localizationCode": "keyboard",
}
_APPLE_KEY = {group: key for key, group in GROUPS.items()}
_DELTA = re.compile(r"-?\d+_\d+")


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
    for key, group in GROUPS.items():
        if key not in groups:
            continue
        try:
            compatible = groups[key]["compatibleOptions"]
            defaults = [value for value, o in compatible.items() if o["isDefault"]]
            if len(defaults) != 1:
                raise OptionsParseError(f"{key}: {len(defaults)} default options, expected 1")
            priced = [o.get("priceDelta") is not None for o in compatible.values()]
            if any(priced) and not all(priced):
                raise OptionsParseError(f"{key}: some options have a priceDelta and some do not")
            base = _delta(compatible[defaults[0]]["priceDelta"]) if all(priced) else None
            for value, o in compatible.items():
                add_on = None if base is None else round(_delta(o["priceDelta"]) - base, 2)
                options.append(Option(group, value, add_on, bool(o["isDefault"]), bool(o["isBlocked"])))
        except (KeyError, TypeError, AttributeError) as exc:
            raise OptionsParseError(f"{key}: unexpected shape ({exc!r})") from exc

    if not any(o.group in ("memory", "storage") for o in options):
        raise OptionsParseError("no memory or storage options in update-config response")
    return options


def _kit(overlay: dict, which: str) -> dict[str, str]:
    dimensions = overlay[which]["kitParts"]["dimensions"]
    return {key: d["dimensionValue"] for key, d in dimensions.items()}


def parse_dependency_switcher(payload: Any, trigger_group: str, trigger_value: str) -> list[Rule]:
    """The forced changes when `trigger_group` is set to `trigger_value`; [] when none."""
    key = _APPLE_KEY.get(trigger_group)
    if key is None:
        raise ValueError(f"unknown option group {trigger_group!r}")
    try:
        overlay = payload["body"]["overlayContent"]
        before, after = _kit(overlay, "currentKit"), _kit(overlay, "selectedKit")
    except (KeyError, TypeError) as exc:
        raise OptionsParseError("no currentKit/selectedKit in dependency-switcher response") from exc
    if after.get(key) != trigger_value:
        raise OptionsParseError(f"selected kit has {key}={after.get(key)!r}, not the requested {trigger_value!r}")
    return [
        Rule(trigger_group, trigger_value, GROUPS[k], v)
        for k, v in after.items()
        if k in GROUPS and k != key and before.get(k) != v
    ]
