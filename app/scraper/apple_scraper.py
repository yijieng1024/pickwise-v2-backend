"""
Apple Malaysia scraper: https://www.apple.com/my/mac/ and each Mac's spec page.

Plain HTTP with Scrapling's `Fetcher`, no browser. The processor reads the spec
text, so `parse_apple_specs` stands in for a browser's `inner_text()`: blocks end
a line, inline text runs together, and script, style and noscript are not text.

Prices are in that text and are the only place Apple prices come from, but the
raw page has only empty slots (`data-pricing-product="macbook-air-13-good"`). A
browser fills them from Apple's price JSON, so `_fetch_prices` does the same:
each `ac:pricing-alias` meta maps a slot to a part number, and the endpoint gives
that part's display price.
"""
import asyncio
import json
import re
from typing import Any, Dict
from urllib.parse import urljoin

from lxml import html as lxml_html

from .fetch import fetch_html as _fetch

_BASE_URL = "https://www.apple.com"
_PRICE_ENDPOINT = _BASE_URL + "/my/shop/mcm/product-price?parts="
_NOT_MACS = ("mac-does-that", "accessories", "displays")
_NOT_TEXT = {"script", "style", "noscript", "template", "source", "img", "svg", "video"}
_BLOCKS = {
    "div", "section", "article", "ul", "ol", "li", "p", "figure", "figcaption", "picture",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr", "dl", "dt", "dd", "br",
}
# Hidden by Apple's CSS at desktop width, or shown only when prices fail to load
_NOT_SHOWN = {"medium-down", "cto-pricing-failed"}
_PRICE_SLOT = "data-pricing-product"
_BREAK = "\0"  # a newline in the source is only whitespace; this ends a line
_SECTIONS = "//*[contains(concat(' ', normalize-space(@class), ' '), ' techspecs-section ')]"


def parse_apple_listing(html: str) -> list[str]:
    """The computed spec page URL of every Mac with a "Learn more" link."""
    doc = lxml_html.fromstring(html)
    links: dict[str, None] = {}
    for anchor in doc.xpath("//a[@href]"):
        if "learn more" not in anchor.text_content().lower():
            continue
        url = urljoin(_BASE_URL, anchor.get("href")).split("?")[0].rstrip("/")
        if any(word in url for word in _NOT_MACS) or not ("/my/mac" in url or "/my/imac" in url):
            continue
        links[url if url.endswith("specs") else f"{url}/specs/"] = None
    return list(links)


def _pricing_aliases(html: str) -> dict[str, str]:
    """Slot name -> part number, from the page's `ac:pricing-alias` metas."""
    doc = lxml_html.fromstring(html)
    aliases = (content.partition("=") for content in doc.xpath("//meta[@name='ac:pricing-alias']/@content"))
    return {slot.strip(): part.strip() for slot, _, part in aliases}


def _fetch_prices(html: str) -> dict[str, str]:
    """Slot name -> the price a browser shows in it."""
    aliases = _pricing_aliases(html)
    if not aliases:
        return {}
    items = json.loads(_fetch(_PRICE_ENDPOINT + ",".join(aliases.values())))["items"]
    return {slot: items[part]["price"]["display"]["smart"] for slot, part in aliases.items() if part in items}


def _collect_text(node, prices: dict[str, str], out: list[str]) -> None:
    if not isinstance(node.tag, str) or node.tag in _NOT_TEXT or node.get("hidden") is not None:
        return
    classes = set((node.get("class") or "").split())
    if classes & _NOT_SHOWN:
        return
    # A visually hidden span is its own text to a reader: "—" then "Not configurable"
    block = node.tag in _BLOCKS or "visuallyhidden" in classes
    if block:
        out.append(_BREAK)
    slot = node.get(_PRICE_SLOT)
    if slot:
        out.append(prices.get(slot, ""))
    out.append(node.text or "")
    for child in node:
        _collect_text(child, prices, out)
        out.append(child.tail or "")
    if block:
        out.append(_BREAK)


def _inner_text(section, prices: dict[str, str]) -> str:
    out: list[str] = []
    _collect_text(section, prices, out)
    lines = (re.sub(r"[ \t\r\n\f]+", " ", line).strip() for line in "".join(out).split(_BREAK))
    return "\n".join(line for line in lines if line)


def _parse_images(doc, page_url: str) -> list[str]:
    images: dict[str, None] = {}
    for src in doc.xpath("//img/@src"):
        url = urljoin(page_url, src)
        lower = url.lower()
        if not any(ext in lower for ext in (".png", ".jpg", ".jpeg", ".webp")):
            continue
        # icons/logos and the /meta/…_og.png social-preview card are not product shots
        if any(bad in lower for bad in ("icon", "logo", "/meta/", "_og.")):
            continue
        images[url] = None
    return list(images)


def parse_apple_specs(html: str, prices: dict[str, str], page_url: str) -> Dict[str, Any]:
    """One spec page, shaped as the bulk pipeline stores it. `status` is
    "failed" when the page has no spec sections."""
    doc = lxml_html.fromstring(html)
    title = doc.findtext(".//title") or ""
    product_name = title.split("-")[0].replace("Buy", "").replace("Tech Specs", "").strip()
    text = "\n\n".join(t for t in (_inner_text(s, prices) for s in doc.xpath(_SECTIONS)) if t)
    data: Dict[str, Any] = {
        "brand": "Apple",
        "product_name": product_name,
        "raw_prices_list": [],
        "image_urls": _parse_images(doc, page_url),
        "raw_specs": [text],
        "status": "success" if text else "failed",
    }
    if not text:
        data["error"] = f"No .techspecs-section on {title.strip()}"
    return data


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def crawl_apple_specs_links(start_url: str = "https://www.apple.com/my/mac/") -> list:
    """Every Mac's Tech Specs URL, computed from the Mac listing page."""
    links = parse_apple_listing(await asyncio.to_thread(_fetch, start_url))
    if not links:
        # A block page is a 200 too: "No URLs found" would look like a quiet day
        raise ValueError(f"No Mac links found on {start_url}")
    return links


async def scrape_official_website(url: str, brand_name: str, brand_id: str) -> Dict[str, Any]:
    """One Mac's spec page. A failure comes back as `status: failed`, never raises."""
    def scrape() -> Dict[str, Any]:
        html = _fetch(url)
        return parse_apple_specs(html, _fetch_prices(html), url)

    try:
        data = await asyncio.to_thread(scrape)
    except Exception as e:
        data = {"brand": brand_name, "status": "failed", "error": str(e)}
    data["source_url"] = url
    if brand_id:
        data["brand_id"] = brand_id
    return data
