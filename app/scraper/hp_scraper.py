"""
HP Malaysia store scraper (https://www.hp.com/my-en/shop/).

Plain HTTP with Scrapling's `Fetcher`, no browser. HP's store is Magento (like
Acer), and each product page is a single SKU (no variants), so a scrape always
returns a one-element list with an empty `source_url_suffix`.

- Specs: the `.product-spec-swapper` sections, section-prefixed like Acer's.
- Name and price: `pagePropertiesData.mainSKU`, an inline JavaScript object read
  without executing it; the finalPrice node is the fallback for the price. The
  part number is in the specs already, as `Product Info / Sku`.
- Photos: the `mage/gallery/gallery` block, which holds only this product's own.
  The page also carries accessory images, so nothing else is read.
"""
import asyncio
import time
from typing import Callable

from lxml import html as lxml_html

from .acer_scraper import _gallery_urls, _parse_prices
from .fetch import fetch_html as _fetch
from .nuxt_state import read_js_object

# ponytail: page walk stops on the first page with no new links; raise if HP
# ever grows past this many listing pages.
_MAX_PAGES = 30

_MAIN_SKU = "pagePropertiesData.mainSKU = {"
_LIVE_PAGE_PAUSE_S = 1.5


def _has_class(name: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def parse_hp_listing(html: str) -> list[str]:
    """Product links on one listing page, without query strings, in page order."""
    doc = lxml_html.fromstring(html)
    return [href.split("?")[0] for href in doc.xpath(f"//a[{_has_class('product-item-link')}]/@href")]


def crawl_hp_listing(fetch_page: Callable[[int], str]) -> list[str]:
    """Walk the pages until one adds nothing new; HP ignores product_list_limit."""
    seen: list[str] = []
    for page_no in range(1, _MAX_PAGES + 1):
        links = parse_hp_listing(fetch_page(page_no))
        if page_no == 1 and not links:
            # A block page is a 200 too: "No URLs found" would look like a quiet day
            raise ValueError("HP listing page 1 has no product links")
        new = [link for link in dict.fromkeys(links) if link not in seen]
        if not new:
            break
        seen += new
    return seen


def _parse_hp_specs(doc) -> dict:
    """Each `.product-spec-swapper` is one section: a title and its <dl> pairs."""
    specs: dict = {}
    for section in doc.xpath(f"//*[{_has_class('product-spec-swapper')}]"):
        titles = section.xpath(f".//*[{_has_class('product-spec-group')}]")
        group = titles[0].text_content().strip() if titles else ""
        for row in section.xpath(f".//dl[{_has_class('product-spec-attribute')}]"):
            labels = row.xpath(f".//*[{_has_class('label')}]")
            values = row.xpath(f".//*[{_has_class('value')}]")
            label = labels[0].text_content().strip() if labels else ""
            value = values[0].text_content().strip() if values else ""
            if label and value:
                specs[f"{group} / {label}" if group else label] = value
    return specs


def _parse_hp_images(doc) -> list[str]:
    urls = [url.split("?")[0] for url in _gallery_urls(doc)]
    return list(dict.fromkeys(url for url in urls if "/catalog/product/" in url))


def parse_hp_product(html: str) -> dict:
    """
    One product page: { status, product_name, specs, image_urls,
    source_url_suffix }. Raises when the page has no spec rows, so a block page
    can never come back as an empty success.
    """
    doc = lxml_html.fromstring(html)
    specs = _parse_hp_specs(doc)
    if not specs:
        raise ValueError("No spec rows found on the page")

    start = html.find(_MAIN_SKU)
    product = read_js_object(html[start + len(_MAIN_SKU) - 1:]) if start >= 0 else {}

    # The browser's dataLayer drops the comma ("PC, Silver"); stored names have none
    product_name = str(product.get("name") or "").replace(",", "").strip()
    if not product_name:
        product_name = specs.get("Product Info / Title", "").split(" - ")[0].strip() or "Unknown Model"

    price = product.get("price") or _parse_prices(doc).get("finalPrice")
    try:
        price_val = float(price)
    except (TypeError, ValueError):
        price_val = 0.0
    specs["Price"] = f"RM{price_val:,.2f}" if price_val > 0 else "N/A"

    return {
        "status": "success",
        "product_name": product_name,
        "specs": specs,
        "image_urls": _parse_hp_images(doc),
        "source_url_suffix": "",
    }


# ---------------------------------------------------------------------------
# Public entry points — same signatures as the ASUS pair
# ---------------------------------------------------------------------------


async def crawl_hp_specs_links(start_url: str) -> list[str]:
    """Return every HP laptop product URL on the listing page (all pages)."""
    base = start_url.split("?")[0]

    def fetch_page(page_no: int) -> str:
        if page_no > 1:
            time.sleep(_LIVE_PAGE_PAUSE_S)
        return _fetch(base if page_no == 1 else f"{base}?p={page_no}")

    return await asyncio.to_thread(crawl_hp_listing, fetch_page)


async def scrape_hp_laptop_specs(url: str, brand_id) -> list[dict]:
    """Scrape one HP product page. Always a single-element list (one SKU/page)."""
    try:
        raw_list: list[dict] = [parse_hp_product(await asyncio.to_thread(_fetch, url))]
    except Exception as e:
        raw_list = [{"status": "failed", "error": str(e)}]

    results: list[dict] = []
    for raw in raw_list:
        if raw.get("status") == "failed":
            results.append(raw)
            continue

        raw_specs = raw.get("specs", {})
        results.append({
            "status": "success",
            "product_name": raw.get("product_name", "Unknown Model"),
            "raw_prices_list": [{"price": raw_specs.get("Price", "N/A")}],
            "image_urls": raw.get("image_urls", []),
            "raw_specs": raw_specs,
            "source_url_suffix": raw.get("source_url_suffix", ""),
        })

    return results


if __name__ == "__main__":  # python -m app.scraper.hp_scraper  (hits the live site)
    import asyncio

    async def _self_check() -> None:
        urls = await crawl_hp_specs_links(
            "https://www.hp.com/my-en/shop/laptops-tablets.html"
        )
        assert len(urls) > 30 and len(set(urls)) == len(urls), len(urls)

        res = await scrape_hp_laptop_specs(urls[0], None)
        assert len(res) == 1, res
        r = res[0]
        assert r["status"] == "success", r
        assert len(r["raw_specs"]) > 10, r["raw_specs"]
        assert r["raw_prices_list"][0]["price"].startswith("RM"), r["raw_prices_list"]
        assert r["image_urls"], "no gallery images"
        print(f"OK — {len(urls)} urls; {r['product_name']} @ {r['raw_prices_list'][0]['price']}")

    asyncio.run(_self_check())
