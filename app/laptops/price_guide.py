"""
Which catalog row each product guide row prices. Pure: `match` takes plain rows
and decides, so the whole decision is tested with no database.

When a match is unclear, nothing is written. A guessed row puts a wrong price on
the live site, while an unmatched row only stays in a visible list, so there is
no best guess and no fuzzy fallback. The admin prices the rest by hand.

Two ways in: the guide's SKU equals the laptop's `manufacturer_sku`, or the model
code plus an exact CPU part number, RAM and storage.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.pickscore.benchmark_service import _normalize

_CLOCK = re.compile(r"\s*\(?\d+(?:\.\d+)?\s*g?hz.*$")
# The part number is the last token with a run of digits: "13420h", "370", "x126100".
_PART = re.compile(r"[a-z]*\d{2,}[a-z]*")
_BASE = re.compile(r"^[A-Z]{1,3}\d{3,4}")


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


@dataclass(frozen=True)
class CatalogRow:
    id: Any
    model_code: str
    product_name: str
    processor_model: Optional[str] = None
    ram_gb: Optional[int] = None
    ssd_gb: Optional[int] = None
    manufacturer_sku: Optional[str] = None


@dataclass
class MatchResult:
    matched: list[tuple[CatalogRow, list[GuideRow]]] = field(default_factory=list)
    ambiguous: list[tuple[GuideRow, list[CatalogRow]]] = field(default_factory=list)
    conflict: list[tuple[CatalogRow, list[GuideRow]]] = field(default_factory=list)
    unmatched_guide: list[GuideRow] = field(default_factory=list)
    unpriced: list[GuideRow] = field(default_factory=list)
    uncovered: list[CatalogRow] = field(default_factory=list)  # needs a price by hand


def _sku_key(sku: Optional[str]) -> str:
    return (sku or "").strip().upper()


def _names(row: CatalogRow, code: str) -> bool:
    """The code as a whole token: P1403 is in "P1403CVA" but not in "GP14035"."""
    pattern = rf"(?<![A-Z0-9]){re.escape(code.upper())}(?!\d)"
    return bool(re.search(pattern, f"{row.model_code} {row.product_name}".upper()))


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
    ]


def match(guide_rows: list[GuideRow], catalog_rows: list[CatalogRow]) -> MatchResult:
    result = MatchResult()
    by_sku = {_sku_key(c.manufacturer_sku): c for c in catalog_rows if c.manufacturer_sku}
    claims: dict[Any, list[GuideRow]] = {}
    rows_by_id = {c.id: c for c in catalog_rows}
    in_doubt: set[Any] = set()

    for guide in guide_rows:
        if guide.price_rm is None:
            result.unpriced.append(guide)
            continue
        hit = by_sku.get(_sku_key(guide.sku))
        candidates = [hit] if hit else _spec_candidates(guide, catalog_rows)
        if not candidates:
            result.unmatched_guide.append(guide)
        elif len(candidates) > 1:
            result.ambiguous.append((guide, candidates))
            in_doubt.update(c.id for c in candidates)
        else:
            claims.setdefault(candidates[0].id, []).append(guide)

    for laptop_id, rows in claims.items():
        same_price = len({r.price_rm for r in rows}) == 1
        (result.matched if same_price else result.conflict).append((rows_by_id[laptop_id], rows))

    result.uncovered = [c for c in catalog_rows if c.id not in claims and c.id not in in_doubt]
    return result
