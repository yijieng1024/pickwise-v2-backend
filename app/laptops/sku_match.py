"""
Which existing laptop a scraped page is another SKU of (plan step 11, decision 15).

One machine is sold under several SKUs: HP has a page and a part number per colour, and
ROG renames a SKU and keeps the specs. When a page's SKU is on no laptop, the processor
asks `same_machine` whether a laptop of the same brand, the same product line and the
same CPU, GPU, RAM, storage and screen size exists, and attaches the SKU to it instead
of inserting a second laptop.

Pure: no database, no LLM, no network. Both sides are read from text the scraper and the
catalog already hold. The page's specs come from its own spec table, never from the
LLM's extraction, so app/scripts/rescrape_dry_run.py, which calls no LLM, decides
exactly as a real run does.
"""
import re
from typing import Any, Iterable, NamedTuple, Optional

# Where each scraper puts a spec in a raw row's `scraped_features`: ROG first, then HP.
# Compared as _key() spells them: HP's ZBook pages write "Memory: / Memory" and
# "Display and graphics: / Graphics".
_PAGE_KEYS = {
    "cpu": ("processor", "processors / processors"),
    "gpu": ("graphics", "display and graphics / graphics"),
    "ram": ("memory", "memory / memory"),
    "storage": ("storage", "storage / hard drive description"),
    "screen": ("display", "display and graphics / display"),
}
_COLOUR_KEYS = ("appearance / product color", "color", "colour")


def _key(name: str) -> str:
    return " ".join(str(name).replace(":", " ").lower().split())


def _stated(scraped_features: Any, keys: tuple) -> Optional[str]:
    """The first non-empty text a page gives under one of *keys*."""
    if not isinstance(scraped_features, dict):
        return None
    by_key = {_key(k): v for k, v in scraped_features.items()}
    return next((by_key[k].strip() for k in keys if isinstance(by_key.get(k), str) and by_key[k].strip()), None)

# The product line ends where the name starts to describe the machine.
_LINE_END = re.compile(
    r"\(|,|\+|\b\d{2}(?:\.\d)?[- ]?inch\b|\b(?:laptop|notebook|mobile workstation|gaming|pc)\b"
)
_MARKS = re.compile(r"[^\x00-\x7f]")  # (R), (TM), and the superscripts they turn into
_CPU_PART = re.compile(r"\b[a-z]{0,2}\d{3,5}[a-z0-9]{0,4}\b")
_GPU_NOISE = re.compile(r"\b(?:nvidia|geforce|laptop|gpu|graphics|processor|integrated)\b")


class Specs(NamedTuple):
    """What must be equal for two listings to be one machine."""
    cpu: str
    gpu: str
    ram_gb: int
    storage_gb: int
    screen_inch: float


def product_line(name: str) -> str:
    """
    The product line in a product name, the page's or the laptop's: the words before the
    first bracket, comma, screen size, or one of Laptop, Notebook, Mobile Workstation,
    Gaming, PC. "HP OmniBook 5 Flip 2-in-1 14 inch Laptop ..." is "hp omnibook 5 flip
    2-in-1"; "ROG Strix G16 (2025) G614 (G614P-WTS178W)" is "rog strix g16".
    """
    text = _MARKS.sub("", name or "").lower()
    match = _LINE_END.search(text)
    return " ".join((text[:match.start()] if match else text).split())


def _cpu(text: Optional[str]) -> str:
    """The CPU's part number ("8940hx", "325", "13650hx"), or the cleaned name if it has none."""
    text = _MARKS.sub("", text or "").lower().split("(")[0]
    text = re.sub(r"\b\d+(?:st|nd|rd|th) gen\b", " ", text)
    parts = _CPU_PART.findall(text)
    return parts[-1] if parts else " ".join(text.replace("processor", " ").split())


def _gpu(text: Optional[str]) -> str:
    """The GPU's name without vendor words: "rtx 5070 ti", "amd radeon 8060s", "intel"."""
    text = _MARKS.sub("", text or "").lower().split(",")[0].split("(")[0]
    return " ".join(_GPU_NOISE.sub(" ", text).split())


def _gb(text: Optional[str]) -> Optional[int]:
    """The first size in *text*, in GB. 1 TB is 1024, as the catalog stores it."""
    match = re.search(r"(\d+)\s?(tb|gb)\b", _MARKS.sub("", text or "").lower())
    if not match:
        return None
    return int(match.group(1)) * (1024 if match.group(2) == "tb" else 1)


def _ram_gb(text: Optional[str]) -> Optional[int]:
    """Memory in GB. "16GB x 2" and "16GB DDR5 SO-DIMM *2" are the total, 32. HP writes the
    total first, "8 GB DDR4-3200 MT/s (1 x 8 GB)", which is 8."""
    size = _gb(text)
    twice = re.search(r"\d+\s?gb\s*(?:ddr\S*\s*)?(?:so-dimm\s*)?[x*]\s?(\d)\b", _MARKS.sub("", text or "").lower())
    return size * int(twice.group(1)) if size and twice else size


def _inch(text: Optional[str]) -> Optional[float]:
    match = re.search(r'(\d{2}(?:\.\d)?)(?:[- ]?inch|")', _MARKS.sub("", text or "").lower())
    return float(match.group(1)) if match else None


def _storage(gb: Optional[int]) -> Optional[int]:
    """1000 and 1024 are both 1 TB in the catalog."""
    return gb if gb is None or gb % 1000 else gb // 1000 * 1024


def page_specs(scraped_features: Any) -> Optional[Specs]:
    """
    The specs a page states in its own spec table, or None when any of the five cannot
    be read (a brand with other keys, a list of text, a missing row). None means "cannot
    say": the processor then matches by slug, as it did before.
    """
    found = {name: _stated(scraped_features, keys) for name, keys in _PAGE_KEYS.items()}
    if not all(found.values()):
        return None
    specs = Specs(_cpu(found["cpu"]), _gpu(found["gpu"]), _ram_gb(found["ram"]), _storage(_gb(found["storage"])),
                  _inch(found["screen"]))
    return None if any(value in (None, "") for value in specs) else specs


def page_colour(scraped_features: Any) -> Optional[str]:
    """The colour name a page gives, if it gives one."""
    return _stated(scraped_features, _COLOUR_KEYS)


def laptop_specs(laptop: Any) -> Specs:
    """The same five, from a stored laptop."""
    screen = laptop.display_size_inch
    return Specs(_cpu(laptop.processor_model), _gpu(laptop.gpu_model), laptop.ram_gb, _storage(laptop.ssd_gb),
                 float(screen) if screen else None)


def same_machine(page_name: str, specs: Specs, laptops: Iterable[Any]) -> Optional[Any]:
    """
    The laptop *page_name* with *specs* is another SKU of, among *laptops* (the brand's
    own, in any status), or None: then the page is a new machine.

    Same product line and the same five specs. The line is required: by specs alone an
    OmniBook X 14 page is an OmniBook 5 Flip. When two laptops fit (a duplicate already in
    the catalog), an active one wins, then the lowest id, so the answer never depends on
    the order the rows came in.
    """
    line = product_line(page_name)
    if not line:
        return None
    fits = [l for l in laptops if product_line(l.product_name) == line and laptop_specs(l) == specs]
    return min(fits, key=lambda l: (l.status != "active", str(l.id))) if fits else None
