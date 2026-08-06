"""
acer_scraper.py
---------------
Offline parser for Acer Malaysia store (store.acer.com) product pages.

Reference product page:
    https://store.acer.com/en-my/acer-swift-light-weight-laptop-swift-go-16-ai-sfg16-i71-75tw

Apple pages are scraped as a DOM text blob and ASUS pages out of
`window.__NUXT__`; this store is Magento 2, so it is a third strategy — the
specs live in the standard Magento attributes table.

**This brand is not fetched over the network.** store.acer.com sits behind
Akamai Bot Manager: it fingerprints the TLS/HTTP2 handshake (Playwright's
bundled Chromium dies with ERR_HTTP2_PROTOCOL_ERROR), and protected paths stay
403 until the `_abck` cookie is validated by telemetry only its obfuscated
sensor JS can produce. Real Chrome and curl_cffi TLS impersonation were both
tried and both refused. So product pages are saved by hand from a normal
browser, uploaded to the API, and stored in the `raw_product_htmls` table;
this module parses the stored string.

The Magento markup is fully server-rendered, so a saved page contains
everything — specs, prices, and the gallery image list — with no JavaScript
needed to reproduce it.

Workflow:
    1. Open a product URL in a normal browser. The catalogue is already queued
       in `laptop_scrape_urls` (GET /scraper/targets?brand_id=… lists it), so
       that table is the list to work through.
    2. Ctrl+S → "Webpage, HTML Only", then POST the file(s) to
       /scraper/upload-html. Filenames do not matter — each page identifies
       itself by its <link rel="canonical"> tag, which is matched back to its
       queued target. Uploading moves the target to `html_uploaded`.
    3. POST /scraper/bulk-scrape (or /scraper/scrape-targets for a subset).
       Parsed targets become `parsed`; targets with no uploaded page come back
       `failed`, which the next bulk run retries — so uploading in batches
       works. /scraper/feed-crawler only needs re-running for products not
       already in the queue.

Crawler  → lists the product URLs that have HTML stored (not the network).
Scraper  → reads a target's stored HTML from the DB and parses it. Product
           pages are Magento *simple* products (no swatches), so one page is
           exactly one SKU / one variant.
"""

import json
import re

from lxml import html as lxml_html
from sqlmodel import Session, select

from app.logger import get_logger
from app.scraper.html_ingest import get_raw_html
from app.scraper.raw_html_model import RawProductHtml

logger = get_logger(__name__)

# Leading bullet/arrow glyphs on Magento section-header rows
_SECTION_PREFIX_RE = re.compile(r"^[▶►◀‣>\s]+")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# HTML parsing (lxml — the Magento markup is fully server-rendered)
# ---------------------------------------------------------------------------


def _clean(node) -> str:
    """Node's text content with whitespace collapsed — `el.textContent` in lxml."""
    if node is None:
        return ""
    return _WHITESPACE_RE.sub(" ", node.text_content()).strip()


def _first(doc, *xpaths: str):
    for xpath in xpaths:
        found = doc.xpath(xpath)
        if found:
            return found[0]
    return None


def _parse_product_name(doc) -> str:
    for xpath in ("//h1[contains(@class,'page-title')]", "//h1"):
        name = _clean(_first(doc, xpath))
        if name:
            return name

    og = doc.xpath("//meta[@property='og:title']/@content")
    return og[0].strip() if og else ""


def _parse_sku(doc) -> str:
    for xpath in (
        "//*[@itemprop='sku']",
        "//div[contains(@class,'product') and contains(@class,'sku')]"
        "//div[contains(@class,'value')]",
    ):
        sku = _clean(_first(doc, xpath))
        if sku:
            return sku
    return ""


def _parse_specs(doc) -> dict[str, str]:
    """
    Read the Magento attributes table.

    Rows are either section headers (a `th` like "▶ Processor & Chipset" with an
    empty `td`) or label/value data rows. Keys are section-prefixed so a generic
    label appearing under several sections cannot collide.
    """
    specs: dict[str, str] = {}
    section = ""

    table = _first(doc, "//table[@id='product-attribute-specs-table']")
    if table is None:
        return specs

    for row in table.xpath(".//tr"):
        label = _SECTION_PREFIX_RE.sub("", _clean(_first(row, ".//th"))).strip()
        value = _clean(_first(row, ".//td"))

        if not label:
            continue
        if not value:
            section = label
            continue

        specs[f"{section} / {label}" if section else label] = value

    return specs


def _parse_prices(doc) -> dict[str, str]:
    """
    Collect `data-price-type` → amount from the main product block.

    Scoped to `.product-info-main` so related-product prices never leak. Nodes
    with an EMPTY data-price-type are instalment/bundle figures and must be
    ignored — pages carry oldPrice, basePrice and an untyped node, so a
    first/last-match pick lands on the wrong number either way.
    """

    def collect(root) -> dict[str, str]:
        found: dict[str, str] = {}
        for node in root.xpath(".//*[@data-price-type]"):
            price_type = (node.get("data-price-type") or "").strip()
            amount = node.get("data-price-amount")
            if not price_type or amount is None or amount == "":
                continue
            found.setdefault(price_type, amount)
        return found

    scope = _first(doc, "//*[contains(@class,'product-info-main')]")
    prices = collect(scope) if scope is not None else {}
    return prices or collect(doc)


# Real product photos all live under this path on Acer's CDN; the gallery
# also carries promo banners, which do not.
_CDN_PRODUCT_PATH = "/media/catalog/product/"


def _parse_image_urls(doc) -> list[str]:
    """
    Product photos.

    Three sources are tried in order and merged, because a page reaches us in
    one of two very different states:

    1. `x-magento-init` JSON — present on a live scrape and on a page saved as
       "Webpage, HTML Only", where the original source survives untouched.
       This is the only source that yields the *whole* gallery.
    2. The og:/twitter: meta tags — a single hero photo, but their absolute
       URLs survive every save mode because browsers do not rewrite <meta>.
    3. Any attribute anywhere holding an absolute CDN product URL. This is the
       net for pages saved as "Webpage, Complete", where the browser rewrites
       every img/@src to a local path inside the sidecar `_files` folder that
       is never uploaded. A couple of gallery nodes keep absolute URLs, and
       they are all that is recoverable from such a save.

    Real photos live under /media/catalog/product/; anything else in the
    gallery is a promo banner. The same photo is served at several sizes via
    ?width=… params, so strip the query to collapse duplicates.

    Note the filter runs *after* all three are collected, not between them. An
    earlier version checked "did this source return anything?" before
    filtering, so a source returning only junk (a save-rewritten local path,
    say) counted as a hit and suppressed the sources that would have worked.
    """
    candidates: list[str] = []

    for script in doc.xpath("//script[@type='text/x-magento-init']"):
        text = script.text or ""
        if "mage/gallery/gallery" not in text:
            continue
        try:
            config = json.loads(text)
        except ValueError:
            continue

        for components in config.values():
            if not isinstance(components, dict):
                continue
            gallery = components.get("mage/gallery/gallery") or {}
            for entry in gallery.get("data") or []:
                if not isinstance(entry, dict):
                    continue
                # Video entries carry a null `full`
                full = entry.get("full") or entry.get("img") or entry.get("thumb")
                if isinstance(full, str):
                    candidates.append(full)

    candidates.extend(doc.xpath("//meta[@property='og:image']/@content"))
    candidates.extend(doc.xpath("//meta[@name='twitter:image']/@content"))
    candidates.extend(doc.xpath("//div[contains(@class,'gallery-placeholder')]//img/@src"))

    # Last resort: sweep every attribute for an absolute CDN product URL.
    for element in doc.iter():
        for value in element.attrib.values():
            if isinstance(value, str) and _CDN_PRODUCT_PATH in value and value.startswith("http"):
                candidates.append(value)

    seen: set[str] = set()
    cleaned: list[str] = []
    for url in candidates:
        if _CDN_PRODUCT_PATH not in url:
            continue
        url = url.split("?")[0]
        if url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned


def _parse_product_page(page_html: bytes | str) -> dict:
    """Extract name / sku / specs / prices / images from one product page."""
    doc = lxml_html.fromstring(page_html)
    return {
        "product_name": _parse_product_name(doc),
        "sku": _parse_sku(doc),
        "specs": _parse_specs(doc),
        "prices": _parse_prices(doc),
        "image_urls": _parse_image_urls(doc),
    }


def _format_price(raw) -> str:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "N/A"
    return f"RM{value:,.2f}" if value > 0 else "N/A"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def crawl_acer_specs_links(
    start_url: str | None = None,
    session: Session | None = None,
    brand_id=None,
) -> list[str]:
    """
    Returns the canonical URL of every Acer page that has HTML stored in
    `raw_product_htmls`.

    Deliberately does NOT hit the network: the store's WAF refuses automated
    requests (see the module docstring), and `robots.txt` disallows `/en-my/*?*`
    — which covers both the `?product_list_limit=all` view and the `?p=N`
    pagination — so even a permitted crawl would only see 10 of 49 products.
    *start_url* is accepted to keep the crawler signature uniform across brands
    but is unused.

    The queue in `laptop_scrape_urls` is normally already populated (uploads
    resolve against it), so this only needs re-running to enqueue a product
    that is not in it yet — the router skips URLs it already has.
    """
    if session is None:
        return []

    filters = [] if brand_id is None else [RawProductHtml.brand_id == brand_id]

    urls = list(
        session.exec(select(RawProductHtml.canonical_url).where(*filters)).all()
    )

    if not urls:
        logger.warning(
            "acer: no stored HTML — upload saved product pages to "
            "POST /scraper/upload-html first."
        )
        return []

    logger.info("acer: %d stored page(s) in raw_product_htmls.", len(urls))
    return urls


async def scrape_acer_laptop_specs(
    url: str, brand_id, session: Session
) -> list[dict]:
    """
    Parses the stored HTML for *url* and extracts its specs from the Magento
    attributes table.

    The page comes from `raw_product_htmls` (uploaded via
    POST /scraper/upload-html), matched on canonical URL — no disk, no network.

    Returns a list of formatted result dicts — always exactly one for Acer,
    since each product page is a single SKU.
    Each dict: { status, product_name, raw_prices_list, image_urls,
                 raw_specs, source_url_suffix }
    On failure: { status: "failed", error: "..." }
    """
    page_html = get_raw_html(session, url, product_type_name="laptop", brand_id=brand_id)

    if page_html is None:
        return [
            {
                "status": "failed",
                "error": (
                    f"No stored HTML for {url}. Open it in a browser, save it as "
                    '"Webpage, HTML Only" and POST it to /scraper/upload-html, '
                    "then re-run — this brand is parsed from uploaded HTML "
                    "because the store's WAF refuses automated requests."
                ),
            }
        ]

    try:
        data = _parse_product_page(page_html)
    except Exception as e:
        return [
            {"status": "failed", "error": f"Could not parse stored HTML for {url} — {e}"}
        ]

    specs: dict = data.get("specs") or {}
    if not specs:
        return [
            {
                "status": "failed",
                "error": (
                    f"No spec rows in the stored HTML for {url} — the save may "
                    "have captured a bot-challenge page instead of the product "
                    "page, or the page layout changed. Re-save and re-upload it."
                ),
            }
        ]

    # Price: finalPrice > basePrice > oldPrice
    prices: dict = data.get("prices") or {}
    price_str = "N/A"
    for key in ("finalPrice", "basePrice", "oldPrice"):
        if key in prices:
            price_str = _format_price(prices[key])
            if price_str != "N/A":
                break
    specs["Price"] = price_str

    # Keep the pre-discount price rather than silently dropping it
    old_price = _format_price(prices["oldPrice"]) if "oldPrice" in prices else "N/A"
    if old_price != "N/A" and old_price != price_str:
        specs["Regular Price"] = old_price

    sku = (data.get("sku") or "").strip()
    if sku:
        specs["SKU"] = sku

    return [
        {
            "status": "success",
            "product_name": data.get("product_name") or "Unknown Model",
            "raw_prices_list": [{"price": price_str}],
            "image_urls": data.get("image_urls") or [],
            "raw_specs": specs,
            # Simple products — one SKU per page, so the bare URL is unique
            "source_url_suffix": "",
        }
    ]
