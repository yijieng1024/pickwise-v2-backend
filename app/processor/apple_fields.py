"""
Apple fields read by code from the specs page, never by the LLM (10a, decided 2026-11-04).

The LLM got these wrong on live rows (a 36GB-max M5 Max stored as 128, a 96W charger as
70W), and each one is stated on the page in a fixed place: a heading, then lines per chip
("M5 Pro with 16-core GPU", "M5 Pro with 18-core CPU or M5 Max") or one unlabelled body.

A field fails when its heading is missing, no line covers this chip, or the lines that
cover it give more than one different value. A failed field is None and is listed in
`field_parse_failures`; it is never handed to the LLM instead.

Pure: no database, no network.
"""
import re
from typing import Mapping, Optional

from app.processor.apple_name import _tidy

# Every field this module owns. fingerprint_reader is parsed for MacBook Neo only.
CODE_FIELDS = ("max_ram_gb", "weight_kg", "power_supply_details", "external_display_support",
               "display_resolution", "fingerprint_reader")

_DASH = r"[-‐‑]"
_CHIP = r"(?:Apple )?([AM]\d+(?: Pro| Max| Ultra)?)"
_ALT = rf"{_CHIP}(?: with (\d+){_DASH}core (CPU|GPU))?"
_LABEL = re.compile(rf"^{_ALT}(?: (?:or|and) {_ALT})*$")
_ALT_FIND = re.compile(_ALT)
_GB = re.compile(r"(\d+)GB(?!/s)")
_ADAPTER = re.compile(r"^\d+W .*Power Adapter.*$")
_CABLE = re.compile(r"^USB-C .*Cable\b.*$")
_RESOLUTION = re.compile(r"(\d{3,4})(?:-by-|x)(\d{3,4}) native resolution")  # Pro "-by-", Neo "x"
_WEIGHT = re.compile(rf"^Weight(?: \((.+)\))?: (\d+(?:\.\d+)?) ?kg\d*$")
_SIZE = re.compile(r"(\d+)(?:\.\d)?-inch \(diagonal\)")


class Failed(ValueError):
    pass


def _block(text: str, *headings: str) -> list[str]:
    """The lines under the first heading found, up to the next blank line. A heading may
    carry a footnote digit ("Storage1")."""
    for heading in headings:
        m = re.search(rf"(?:^|\n){re.escape(heading)}\d*\n", text)
        if m:
            return [line for line in text[m.end():].split("\n\n")[0].split("\n") if line]
    raise Failed(f"no '{headings[0]}' heading")


def _covers(label: str, config: Mapping) -> bool:
    for chip, cores, kind in _ALT_FIND.findall(label):
        if chip == config["chip"] and (not cores or int(cores) == config[f"{kind.lower()}_cores"]):
            return True
    return False


def _chip_lines(lines: list[str], config: Mapping) -> list[list[str]]:
    """The bodies that apply to this chip: every labelled body that covers it, or the
    whole block when it has no chip labels."""
    if not any(_LABEL.match(line) for line in lines):
        return [lines]
    bodies, current = [], None
    for line in lines:
        if _LABEL.match(line):
            current = [] if _covers(line, config) else None
            if current is not None:
                bodies.append(current)
        elif current is not None:
            current.append(line)
    if not bodies:
        raise Failed(f"no line for {config['chip']}")
    return bodies


def _one(values: list) -> object:
    found = set(values)
    if not found:
        raise Failed("no value")
    if len(found) > 1:
        raise Failed(f"more than one value: {sorted(map(str, found))}")
    return found.pop()


def _columns(lines: list[str], starts: re.Pattern) -> list[list[str]]:
    cols: list[list[str]] = []
    for line in lines:
        if starts.match(line):
            cols.append([])
        if cols:
            cols[-1].append(line)
    return cols


def _max_ram(text: str, config: Mapping) -> int:
    def most(lines):
        return max((int(g) for line in lines for g in _GB.findall(line)), default=None)
    values = []
    for body in _chip_lines(_block(text, "Memory"), config):
        # An unlabelled body is one column per configuration, each opening on its base size
        # followed by "<size> unified memory"; a lone "32GB" after "Configurable to:" is an
        # option inside a column, not a new one (Air, 2026-11-04).
        opens = {line for line, nxt in zip(body, body[1:]) if re.fullmatch(r"\d+GB", line)
                 and nxt.startswith(f"{line} unified memory")}
        cols = _columns(body, re.compile("^(?:" + "|".join(map(re.escape, opens)) + ")$")) if opens else [body]
        values += [v for v in map(most, cols) if v is not None]
    return _one(values)


def _weight(text: str, config: Mapping) -> float:
    values = []
    for line in _block(text, "Size and Weight"):
        m = _WEIGHT.match(line)
        if m and (m.group(1) is None or _covers(m.group(1), config)):
            values.append(float(m.group(2)))
    if not values:
        raise Failed(f"no weight line for {config['chip']}")
    return _one(values)


def _charger(text: str, config: Mapping) -> str:
    """The adapter and the charging cable from In the Box (decided 2026-11-04), e.g.
    "70W USB-C Power Adapter, USB-C to MagSafe 3 Cable (2m)". Not the laptop itself,
    not the polishing cloth."""
    values = []
    for body in _chip_lines(_block(text, "In the Box"), config):
        adapter = _one([line for line in body if _ADAPTER.match(line)])
        try:
            cable = _one([line for line in body if _CABLE.match(line)])
        except Failed as e:
            raise Failed(f"cable: {e}")
        values.append(f"{adapter}, {cable}")
    return _one(values)


def _display_line(line: str) -> Optional[str]:
    """An external-display line, tidied, or None (decided 2026-11-04): kept are how many
    displays, their resolution and refresh, and the ports they use; dropped are the
    built-in display, port speed lines and links. A footnote digit after a unit goes
    ("60Hz4"), and so does what only joined it to the next line (":", "or")."""
    if not re.search(r"\bdisplays?\b", line) or "built-in" in line or "speeds" in line or line.startswith("Learn more"):
        return None
    line = re.sub(r"(?<=[a-z])\d{1,2}$", "", line)
    return re.sub(r"(?:\s+or|\s*[:;,])+$", "", line).strip()


def _displays(text: str, config: Mapping) -> str:
    values = []
    for body in _chip_lines(_block(text, "External Display Support", "Display Support"), config):
        kept = [k for k in map(_display_line, body) if k]
        if not kept:
            raise Failed("no external display line")
        values.append("; ".join(kept))
    return _one(values)


def _resolution(text: str, config: Mapping) -> str:
    return _one(["x".join(m) for line in _block(text, "Display") for m in _RESOLUTION.findall(line)])


def _touch_id(text: str, config: Mapping) -> bool:
    """MacBook Neo: the Keyboard and Trackpad block has one column per storage column,
    and only one of them has Touch ID ("Not available, Touch ID" in the other)."""
    storage = _columns(_block(text, "Storage"), re.compile(r"^\d+(?:GB|TB)$"))
    keyboard = _columns(_block(text, "Keyboard and Trackpad"), re.compile(r"^Magic Keyboard"))
    if not storage or len(storage) != len(keyboard):
        raise Failed(f"{len(storage)} storage columns but {len(keyboard)} keyboard columns")
    sizes = [int(c[0][:-2]) * (1024 if c[0].endswith("TB") else 1) for c in storage]
    if sizes.count(config["ssd_gb"]) != 1:
        raise Failed(f"no single storage column for {config['ssd_gb']}GB")
    col = keyboard[sizes.index(config["ssd_gb"])]
    has = any(line == "Touch ID" or line.endswith("with Touch ID") for line in col)
    lacks = any("Not available, Touch ID" in line for line in col)
    return _one([v for v, seen in ((True, has), (False, lacks)) if seen])


_PARSERS = {
    "max_ram_gb": _max_ram,
    "weight_kg": _weight,
    "power_supply_details": _charger,
    "external_display_support": _displays,
    "display_resolution": _resolution,
}


def section_for(sections: list[str], size) -> Optional[str]:
    """The size section a configuration belongs to, by its Display line; a one-size page
    is its own section. None when not exactly one section is that size."""
    if len(sections) == 1:
        return sections[0]
    found = [s for s in sections if (m := _SIZE.search(s)) and int(m.group(1)) == int(size)]
    return found[0] if len(found) == 1 else None


def parse(section: Optional[str], config: Mapping, product: str) -> tuple[dict, list[dict]]:
    """The code-parsed fields for one configuration, and one failure per field that could
    not be read. Every field in the result is set; a failed one is None."""
    parsers = dict(_PARSERS)
    if product == "MacBook Neo":
        parsers["fingerprint_reader"] = _touch_id
    fields, failures = {}, []
    for field, parser in parsers.items():
        try:
            if section is None:
                raise Failed(f"no specs section for {config['size']}-inch")
            fields[field] = parser(_tidy(section), config)
        except Failed as e:
            fields[field] = None
            failures.append({"field": field, "reason": str(e)})
    return fields, failures
