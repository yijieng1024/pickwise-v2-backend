"""Shopee / Lazada search links for a catalog laptop (search only, no prices).

The search text is the product line plus the manufacturer model code taken
from product_name, without RAM/storage noise. Spot-checked on both sites
(2026-10-25):
- HP / Acer: the FULL SKU (14-fr0020TU, A715-59G-54Q6). Cutting it back to a
  series (15-fc0) filled the top results with weaker-CPU siblings.
- Asus: the CHASSIS code (GU605, G835), i.e. the part before the hyphen.
  A full Asus SKU found the exact machine only when sellers still list that
  exact SKU; otherwise it returned noisier results than the chassis.
- No code in the name: product-line head plus the CPU.
- Apple: family name plus chip (configurations are picked inside a listing).
"""
import re
from typing import Optional
from urllib.parse import quote_plus

# A model-code candidate: letters and digits, optionally hyphenated.
_TOKEN = re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")
_SKIP = re.compile(r"^\d+(gb|tb|hz|wh?)$|^2-in-1$|^\d+-(inch|core)$", re.I)
_FILLER = re.compile(
    r"\b\d+(\.\d+)?(-| )?inch\b|\d+(\.\d+)?(\"|'')|\b(2-in-1|laptop|notebook|"
    r"mobile workstation|next gen|copilot\+|ai|pc)\b|\b20\d\d\b",
    re.I,
)
_CPU_NOISE = re.compile(r"\b(intel|amd|processor)\b|[™®]", re.I)
_APPLE_CHIP = re.compile(r"\b(M\d(?: (?:Pro|Max))?|A\d+ Pro)\b")


def _model_code(product_name: str, cpu: str, gpu: str) -> Optional[str]:
    hardware = f"{cpu} {gpu}".lower()
    for token in _TOKEN.findall(product_name):
        digits = sum(ch.isdigit() for ch in token)
        if digits < 3 or not any(ch.isalpha() for ch in token):
            continue
        if _SKIP.match(token) or token.lower() in hardware:
            continue
        return token
    return None


def _clean(text: str) -> str:
    return " ".join(_FILLER.sub(" ", text).replace("(", " ").split())


def search_text(
    brand: str,
    product_name: str,
    processor_model: Optional[str],
    gpu_model: Optional[str],
    family_name: Optional[str] = None,
) -> str:
    cpu, gpu = processor_model or "", gpu_model or ""
    if brand.lower() == "apple":
        chip = _APPLE_CHIP.search(cpu) or _APPLE_CHIP.search(product_name)
        line = family_name or _clean(re.split(r"\(", product_name)[0])
        return f"{line} {chip.group(1)}" if chip else line

    code = _model_code(product_name, cpu, gpu)
    cut = len(product_name)
    for sep in ("(", " - ", ","):
        i = product_name.find(sep)
        if i != -1:
            cut = min(cut, i)
    if code:
        cut = min(cut, product_name.find(code))
        if brand.lower() == "asus":
            code = code.split("-")[0]
    head = _clean(product_name[:cut])
    if not head.lower().startswith(brand.lower()):
        head = f"{brand} {head}"
    tail = code or " ".join(_CPU_NOISE.sub(" ", cpu).split())
    return f"{head} {tail}".strip()


def marketplace_links(query: str) -> dict:
    encoded = quote_plus(query)
    return {
        "shopee_search_url": f"https://shopee.com.my/search?keyword={encoded}",
        "lazada_search_url": f"https://www.lazada.com.my/catalog/?q={encoded}",
    }
