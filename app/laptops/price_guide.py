"""
Which catalog row each product guide row prices. Pure: `match` takes plain rows
and decides, so the whole decision is tested with no database.

When a match is unclear, nothing is written. A guessed row puts a wrong price on
the live site, while an unmatched row only stays in a visible list, so there is
no best guess and no fuzzy fallback. The admin prices the rest by hand.

Two ways in: the guide's SKU equals the laptop's `manufacturer_sku`, or the model
code plus an exact CPU part number, RAM and storage. A match that found the laptop
only through the base code (S5406 for S5406S) waits for an admin: a base code is the
whole line, and the catalog's S5406M took the S5406S's row that way. Whichever way it
matched, a laptop whose panel plainly differs from the guide's is a conflict.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.pickscore.benchmark_service import _normalize

_CLOCK = re.compile(r"\s*\(?\d+(?:\.\d+)?\s*g?hz.*$")
# The part number is the last token with a run of digits: "13420h", "370", "x126100".
_PART = re.compile(r"[a-z]*\d{2,}[a-z]*")
_BASE = re.compile(r"^[A-Z]{1,3}\d{3,4}")
_NOT_OLED = re.compile(r"\bips\b|\bled\b|\blcd\b")  # LED backlit and Mini LED are backlit panels


def cpu_part(text: Optional[str]) -> Optional[str]:
    """A comparable key for one CPU. Exact on the suffix (13620H is not 13620HX)
    and on the line (Ryzen 7 170 is not Ryzen AI 7 170, 290HX is not 290HX Plus),
    deaf to how a source spells the rest ("i7-14650HX", "i7 Processor 14650HX").
    """
    if not text:
        return None
    # _normalize is the 6b work: it folds "Processor", case, and the four spellings
    # of a Qualcomm part number.
    normal = _CLOCK.sub("", _normalize(text))
    parts = _PART.findall(normal)
    if not parts:
        return None
    part = parts[-1]
    if re.search(rf"\bhx {part}\b", normal):
        part = f"hx{part}"
    if f"{parts[-1]} plus" in normal:
        part += " plus"
    # Snapdragon first: "(18-core)" must not read as the Core line.
    for line in ("snapdragon", "ryzen ai max", "ryzen ai", "ryzen", "ultra", "core", "celeron"):
        if line in normal:
            return f"{line}:{part}"
    return part


@dataclass(frozen=True)
class GuideRow:
    sku: str
    model_code: str
    price_rm: Optional[float] = None
    cpu: Optional[str] = None
    ram_gb: Optional[int] = None
    ssd_gb: Optional[int] = None
    page: Optional[int] = None
    panel: Optional[str] = None


@dataclass(frozen=True)
class CatalogRow:
    id: Any
    model_code: str
    product_name: str
    processor_model: Optional[str] = None
    ram_gb: Optional[int] = None
    ssd_gb: Optional[int] = None
    manufacturer_sku: Optional[str] = None
    panel: Optional[str] = None


@dataclass
class MatchResult:
    matched: list[tuple[CatalogRow, list[GuideRow]]] = field(default_factory=list)
    ambiguous: list[tuple[GuideRow, list[CatalogRow]]] = field(default_factory=list)
    conflict: list[tuple[CatalogRow, list[GuideRow]]] = field(default_factory=list)
    suffix_rule: list[tuple[CatalogRow, list[GuideRow]]] = field(default_factory=list)  # never written by an apply
    base_code: list[tuple[CatalogRow, list[GuideRow]]] = field(default_factory=list)  # nor these
    unmatched_guide: list[GuideRow] = field(default_factory=list)
    unpriced: list[GuideRow] = field(default_factory=list)
    uncovered: list[CatalogRow] = field(default_factory=list)  # in no row of this guide


def _sku_key(sku: Optional[str]) -> str:
    """-W and -WP read as one SKU (Jack, 2026-11-02). Only those two: -WS and -WSP stay
    apart. No Q3 guide prints both forms of a SKU, so nothing proves this: a match that
    needs it waits for the admin, and if a later guide prints both at two prices, the
    row is a conflict and writes nothing."""
    return re.sub(r"WP$", "W", (sku or "").strip().upper())


def _needs_suffix_rule(row: CatalogRow, guide_rows: list[GuideRow]) -> bool:
    """A SKU match that holds only because -W and -WP read as one."""
    stored = (row.manufacturer_sku or "").strip().upper()
    return bool(stored) and all(guide.sku.strip().upper() != stored for guide in guide_rows)


def _names(row: CatalogRow, code: str) -> bool:
    """The code as a whole token: P1403 is in "P1403CVA" but not in "GP14035"."""
    pattern = rf"(?<![A-Z0-9]){re.escape(code.upper())}(?!\d)"
    return bool(re.search(pattern, f"{row.model_code} {row.product_name}".upper()))


def panel_kind(text: Optional[str]) -> Optional[str]:
    """"oled", "other", or None when the text states neither. A blank guide cell is
    not a statement (Jack, 2026-11-02), and "Anti-glare display" fits both kinds."""
    low = (text or "").lower()
    if "oled" in low:
        return "oled"
    return "other" if _NOT_OLED.search(low) else None


def panels_differ(row: CatalogRow, guide_rows: list[GuideRow]) -> bool:
    """Both sides state a panel, and they do not agree."""
    stored = panel_kind(row.panel)
    return stored is not None and any(panel_kind(g.panel) not in (None, stored) for g in guide_rows)


def _needs_base_code(row: CatalogRow, guide_rows: list[GuideRow]) -> bool:
    """A spec match on a laptop that names none of its guide rows' full model codes."""
    return not row.manufacturer_sku and not any(_names(row, guide.model_code) for guide in guide_rows)


def _letter_fits(guide: GuideRow, row: CatalogRow) -> bool:
    """FX608J-MRV040W is the FX608JM, not the FX608JP: the letter after the hyphen
    continues the catalog's code. It carries the GPU, which guide rows do not state."""
    letter = guide.sku.partition("-")[2][:1].upper()
    code = re.escape(guide.model_code.upper())
    found = re.findall(rf"(?<![A-Z0-9]){code}([A-Z])", f"{row.model_code} {row.product_name}".upper())
    return all(next_letter == letter for next_letter in found)


def _spec_candidates(guide: GuideRow, catalog: list[CatalogRow]) -> list[CatalogRow]:
    part = cpu_part(guide.cpu)
    if part is None or guide.ram_gb is None or guide.ssd_gb is None:
        return []  # a row with no CPU, RAM or storage cannot use this path
    named = [c for c in catalog if _names(c, guide.model_code)]
    if not named:
        base = _BASE.match(guide.model_code)
        named = [c for c in catalog if _names(c, base.group(0))] if base else []
    return [
        c for c in named
        if cpu_part(c.processor_model) == part and c.ram_gb == guide.ram_gb and c.ssd_gb == guide.ssd_gb
        and _letter_fits(guide, c)
    ]


def match(guide_rows: list[GuideRow], catalog_rows: list[CatalogRow]) -> MatchResult:
    result = MatchResult()
    by_sku: dict[str, list[CatalogRow]] = {}
    for row in catalog_rows:
        if row.manufacturer_sku:
            by_sku.setdefault(_sku_key(row.manufacturer_sku), []).append(row)
    # A laptop with a SKU of its own is priced by that SKU or not at all: a guide row
    # with another SKU is another machine, however alike the specs read.
    no_sku = [c for c in catalog_rows if not c.manufacturer_sku]
    claims: dict[Any, list[GuideRow]] = {}
    rows_by_id = {c.id: c for c in catalog_rows}
    in_doubt: set[Any] = set()

    for guide in guide_rows:
        if guide.price_rm is None:
            result.unpriced.append(guide)
            continue
        hits = by_sku.get(_sku_key(guide.sku))
        if hits:  # one row prices the -W and -WP laptops alike, so this is never ambiguous
            for hit in hits:
                claims.setdefault(hit.id, []).append(guide)
            continue
        candidates = _spec_candidates(guide, no_sku)
        if not candidates:
            result.unmatched_guide.append(guide)
        elif len(candidates) > 1:
            result.ambiguous.append((guide, candidates))
            in_doubt.update(c.id for c in candidates)
        else:
            claims.setdefault(candidates[0].id, []).append(guide)

    for laptop_id, rows in claims.items():
        row = rows_by_id[laptop_id]
        if len({r.price_rm for r in rows}) > 1 or panels_differ(row, rows):
            result.conflict.append((row, rows))
        elif _needs_suffix_rule(row, rows):
            result.suffix_rule.append((row, rows))
        elif _needs_base_code(row, rows):
            result.base_code.append((row, rows))
        else:
            result.matched.append((row, rows))

    result.uncovered = [c for c in catalog_rows if c.id not in claims and c.id not in in_doubt]
    return result
