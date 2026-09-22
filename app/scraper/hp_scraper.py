"""
HP Malaysia store scraper (https://www.hp.com/my-en/shop/).

Same shape as `asus_scraper.py` — live Playwright crawl of the listing page,
then a per-product scrape — but HP's store is Magento (like Acer), so the
specs come from the rendered `#specifications-div` DOM rather than a JSON
state blob, and each product page is a single SKU (no variants), so the
scrape always returns a one-element list with an empty `source_url_suffix`.

Price and marketing name come from the GTM `dataLayer` `e_pageView` event,
which carries the numeric price and the HP part number (e.g. `E0GM0PA`);
the DOM price node is the fallback.
"""


from playwright.async_api import async_playwright

from .playwright_utils import run_async_playwright

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ponytail: page walk stops on the first page with no new links; raise if HP
# ever grows past this many listing pages.
_MAX_PAGES = 30

# Every `.product-spec-swapper` is one section: a title div followed by a div
# of <dl> label/value pairs. Keys are section-prefixed like the Acer scraper.
_SPECS_JS = """
() => {
    const out = {};
    for (const sw of document.querySelectorAll('.product-spec-swapper')) {
        const group = (sw.querySelector('.product-spec-group')?.textContent || '').trim();
        for (const dl of sw.querySelectorAll('dl.product-spec-attribute')) {
            const label = (dl.querySelector('.label')?.textContent || '').trim();
            const value = (dl.querySelector('.value')?.textContent || '').trim();
            if (label && value) out[group ? `${group} / ${label}` : label] = value;
        }
    }
    return out;
}
"""

# The gallery placeholder holds only this product's photos — the page also
# carries accessory/recommendation images, which must not be picked up.
# The same photo is served from several `/cache/<hash>/` dirs (one per size
# preset), so collapse on the path after the hash to dedupe.
_IMAGES_JS = r"""
() => {
    const seen = new Map();
    for (const i of document.querySelectorAll(
            '[data-gallery-role="gallery-placeholder"] img, .fotorama__img, .fotorama__stage img')) {
        const u = (i.src || i.getAttribute('data-src') || '').split('?')[0];
        if (!u.includes('/catalog/product/')) continue;
        const key = u.replace(/\/cache\/[0-9a-f]{32}\//, '/');
        if (!seen.has(key)) seen.set(key, u);
    }
    return [...seen.values()];
}
"""

_PRODUCT_JS = """
() => {
    for (const e of (window.dataLayer || []).slice().reverse()) {
        const p = e?.ecommerce?.detail?.products?.[0];
        if (p) return p;
    }
    return null;
}
"""


async def _async_crawl_hp_specs_links(start_url: str) -> list[str]:
    """Walk `?p=N` until a page adds nothing new; HP ignores product_list_limit."""
    seen: list[str] = []
    seen_set: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        base = start_url.split("?")[0]
        for page_no in range(1, _MAX_PAGES + 1):
            url = base if page_no == 1 else f"{base}?p={page_no}"
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2000)

            hrefs = await page.eval_on_selector_all(
                "a.product-item-link", "els => els.map(e => e.href)"
            )
            new = [h.split("?")[0] for h in hrefs if h.split("?")[0] not in seen_set]
            if not new:
                break
            for h in new:
                seen_set.add(h)
                seen.append(h)

        await browser.close()

    return seen


async def _async_scrape_hp_laptop_specs(url: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            # `state="attached"` — the spec sections render collapsed/hidden
            # until the user expands them, so they are never "visible".
            await page.wait_for_selector(
                ".product-spec-swapper", state="attached", timeout=30000
            )

            specs: dict = await page.evaluate(_SPECS_JS)
            if not specs:
                raise Exception(f"No spec rows found at {url}.")

            product: dict | None = await page.evaluate(_PRODUCT_JS)
            product = product or {}

            product_name = (product.get("name") or "").strip()
            if not product_name:
                product_name = (
                    specs.get("Product Info / Title", "").split(" - ")[0].strip()
                    or "Unknown Model"
                )

            price = product.get("price")
            if not price:
                # DOM fallback: Magento's final price node.
                text = await page.evaluate(
                    "() => document.querySelector('[data-price-type=\"finalPrice\"]')"
                    "?.getAttribute('data-price-amount') || ''"
                )
                price = text.strip()
            try:
                price_val = float(price)
            except (TypeError, ValueError):
                price_val = 0.0
            specs["Price"] = f"RM{price_val:,.2f}" if price_val > 0 else "N/A"

            # Fotorama renders one placeholder <img> immediately and fills
            # the rest of the gallery seconds later, so reading right after
            # the specs yields exactly one photo. Wait for it to settle.
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('.fotorama__img').length > 1",
                    timeout=15000,
                )
            except Exception:  # noqa: BLE001 - some SKUs genuinely have one photo
                pass
            image_urls: list[str] = await page.evaluate(_IMAGES_JS)

            await browser.close()
            return [{
                "status": "success",
                "product_name": product_name,
                "specs": specs,
                "image_urls": list(dict.fromkeys(image_urls)),
                "source_url_suffix": "",
            }]

        except Exception as e:
            await browser.close()
            return [{"status": "failed", "error": str(e)}]


# ---------------------------------------------------------------------------
# Public entry points — same signatures as the ASUS pair
# ---------------------------------------------------------------------------


async def crawl_hp_specs_links(start_url: str) -> list[str]:
    """Return every HP laptop product URL on the listing page (all pages)."""
    return await run_async_playwright(_async_crawl_hp_specs_links(start_url))


async def scrape_hp_laptop_specs(url: str, brand_id) -> list[dict]:
    """Scrape one HP product page. Always a single-element list (one SKU/page)."""
    raw_list: list[dict] = await run_async_playwright(
        _async_scrape_hp_laptop_specs(url)
    )

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
