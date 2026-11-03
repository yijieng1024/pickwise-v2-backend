"""
Apple identity, built by code: which MacBook Pro line a page is, the line token in
`model_code`, the `product_name`, and the fields a model_code match must agree on.

Why code and not the LLM (2026-11-03): the PROLED XDR and Liquid Retina XDR MacBook
Pros share size, chip, cores, RAM and storage, so the LLM's slug named both the same.
The PROLED page then overwrote a June Liquid Retina row in place, and the LLM's names
("MacBook Pro with PROLED XDR Display") could not tell any of the configurations apart.
The line lives only in the page URL, so the URL decides it.
"""
import re
from typing import Optional
from urllib.parse import urlparse

# The path after /macbook-pro/ → (token in model_code, label in product_name). Exact
# paths only: anything else is unknown. "specs" alone is the old single page: the
# Liquid Retina XDR machine (same chassis, weights and battery as today's
# /liquid-retina-xdr-display/ page).
_PRO_LINES = {
    "proled-xdr-display/specs": ("proled", "PROLED XDR"),
    "liquid-retina-xdr-display/specs": ("liquid-retina", "Liquid Retina XDR"),
    "specs": ("liquid-retina", "Liquid Retina XDR"),
}
_LABELS = {token: label for token, label in _PRO_LINES.values()}

_CHIP_RE = re.compile(r"\b[AM]\d+(?: (?:Pro|Max|Ultra))?\b")


class UnknownLine(ValueError):
    """An Apple page this module cannot place. Never guessed: guessing is how a
    PROLED page became a Liquid Retina row."""


def product_and_line(source_url: str) -> tuple[str, Optional[str]]:
    """("MacBook Pro", "proled"), ("MacBook Air", None). Raises UnknownLine for a
    MacBook Pro page whose line is not known, or a page that is no MacBook at all."""
    parts = [p for p in urlparse(source_url or "").path.split("/") if p]
    for i, part in enumerate(parts):
        if not part.startswith("macbook-"):
            continue
        product = "MacBook " + part.split("-", 1)[1].title()
        if part != "macbook-pro":
            return product, None
        segment = "/".join(parts[i + 1:])
        if segment not in _PRO_LINES:
            raise UnknownLine(f"unknown MacBook Pro line: {source_url}")
        return product, _PRO_LINES[segment][0]
    raise UnknownLine(f"not a MacBook page: {source_url}")


def with_line_token(model_code: str, line: Optional[str]) -> str:
    """The LLM's slug with the line after the size: apple-macbook-pro-14-proled-m5-…"""
    code = model_code.lower()
    if not line or line_of_code(code) == line:
        return code
    return re.sub(r"^(apple-macbook-pro(?:-\d+)?)-", rf"\1-{line}-", code, count=1)


def line_of_code(model_code: str) -> Optional[str]:
    return next((t for t in _LABELS if f"-{t}-" in model_code.lower()), None)


def chip_of(processor_model: Optional[str]) -> Optional[str]:
    """"Apple M5 Pro (18-core)" and "Apple M5 Pro chip" are both "M5 Pro"."""
    match = _CHIP_RE.search(processor_model or "")
    return match.group(0) if match else None


# Peak SDR brightness per MacBook Pro line (decided 2026-11-03): both pages say "SDR
# brightness: up to 1,000 nits", and PROLED adds "2,000 nits peak". Air and Neo have one
# line each and keep the extracted figure.
_BRIGHTNESS = {"proled": 2000, "liquid-retina": 1000}


def processor_name(processor_model: Optional[str], cpu_cores: Optional[int]) -> Optional[str]:
    """The June form, "Apple M5 Pro (15-core)": the one the benchmark table matches. The
    LLM writes "Apple M5 Pro chip" or "Apple M5 Pro chip (15-core)" depending on the page.
    None when the chip or the core count is missing: the caller skips that config rather
    than store the LLM's wording, which the benchmark table may not match."""
    chip = chip_of(processor_model)
    return f"Apple {chip} ({cpu_cores}-core)" if chip and cpu_cores else None


def brightness(line: Optional[str], extracted: Optional[int]) -> Optional[int]:
    return _BRIGHTNESS.get(line, extracted) if line else extracted


def _storage(ssd_gb: Optional[int]) -> Optional[str]:
    if not ssd_gb:
        return None
    return f"{ssd_gb // 1024}TB SSD" if ssd_gb % 1024 == 0 else f"{ssd_gb}GB SSD"


def build_name(
    product: str,
    line: Optional[str],
    display_size_inch: Optional[float],
    processor_model: Optional[str],
    cpu_cores: Optional[int],
    gpu_cores: Optional[int],
    ram_gb: Optional[int],
    ssd_gb: Optional[int],
) -> str:
    """14-inch MacBook Pro PROLED XDR (M5 Pro, 15-core CPU, 16-core GPU, 24GB RAM, 2TB SSD)"""
    size = f"{int(display_size_inch)}-inch " if display_size_inch else ""
    label = f" {_LABELS[line]}" if line else ""
    specs = [
        chip_of(processor_model),
        f"{cpu_cores}-core CPU" if cpu_cores else None,
        f"{gpu_cores}-core GPU" if gpu_cores else None,
        f"{ram_gb}GB RAM" if ram_gb else None,
        _storage(ssd_gb),
    ]
    return f"{size}{product}{label} ({', '.join(s for s in specs if s)})"


def identity(display_size_inch: Optional[float], processor_model: Optional[str], model_code: str) -> dict:
    """What a model_code match must agree on before it may overwrite a row."""
    return {
        "screen_inch": int(display_size_inch) if display_size_inch else None,
        "chip": chip_of(processor_model),
        "line": line_of_code(model_code),
    }


# Size sections per product page. The page lists each size's specs in turn, and before
# 2026-11-03 the processor sent only the first 8,000 characters of the whole dump, so
# every second size (16-inch MacBook Pro, 15-inch Air) never reached the LLM.
_SIZES = {"MacBook Pro": 2, "MacBook Air": 2}
SECTION_LIMIT = 12_000


class SplitFailed(ValueError):
    """The page does not have the size sections it should. Never sent as one blob
    instead: that is the silent truncation this split exists to end."""


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def size_sections(text: str, product: str) -> list[str]:
    """One text per size: its specs up to "Operating System" (the rest is the macOS app
    list, accessibility and environment copy), plus its "In the Box" paragraph. A page
    with one size is returned whole."""
    expected = _SIZES.get(product, 1)
    if expected == 1:
        return [text]
    text = _tidy(text)
    starts = [m.start() for m in re.finditer(r"\nFinish\n", text)]
    weights = text.count("\nSize and Weight\n")
    if len(starts) != expected or weights != expected:
        raise SplitFailed(
            f"{product}: expected {expected} size sections, found {len(starts)} 'Finish' "
            f"and {weights} 'Size and Weight' headings"
        )
    sections = []
    for i, start in enumerate(starts):
        section = text[start: starts[i + 1] if i + 1 < len(starts) else len(text)]
        if section.count("\nSize and Weight\n") != 1:
            raise SplitFailed(f"{product}: size section {i + 1} has no 'Size and Weight' of its own")
        stop = section.find("\nOperating System\n")
        box = section.find("\nIn the Box\n")
        core = (section[:stop] if stop > 0 else section) + (section[box:].split("\n\n")[0] if box > 0 else "")
        if len(core) > SECTION_LIMIT:
            raise SplitFailed(f"{product}: size section {i + 1} is {len(core)} characters, over {SECTION_LIMIT}")
        sections.append(core)
    return sections
