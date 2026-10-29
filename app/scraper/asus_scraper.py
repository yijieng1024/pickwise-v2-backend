"""
ASUS Malaysia scraper: www.asus.com and rog.asus.com, one brand (`asus`).

- Discovery: ASUS's own listing API (`SeriesFilterResult`), paged, which lists
  www and ROG laptops together. No browser.
- www.asus.com spec pages (Nuxt 3): fetched with Scrapling's `Fetcher` and read
  from the `__NUXT_DATA__` payload (`pinia.pdPageStore`) by `parse_asus_techspec`.
  No price: www prices come only from the product guides (step 10, Phase 3).
- rog.asus.com spec pages (Nuxt 2): still Playwright, reading
  `window.__NUXT__.state.Spec.spec`, until step 10 Phase 2a moves them over.
"""
import asyncio
import json
import re
from typing import Callable

from playwright.async_api import async_playwright
from scrapling.fetchers import Fetcher

from .nuxt_state import nuxt3_state
from .playwright_utils import run_async_playwright

# Listing API behind https://www.asus.com/my/laptops/all-products/ -- a module
# constant, so the crawl target is never client-supplied
_SERIES_API = (
    "https://odinapi.asus.com/recent-data/apiv2/SeriesFilterResult?CategoryName=&PageIndex={page}"
    "&PageSize={size}&PriceMax=&PriceMin=&ProductLevel1Code=laptops&ProductLevel2Code=&SeriesName="
    "&Sort=&Spec=&SubSeriesName=&SubSpec=&SystemCode=asus&WebsiteCode=my&siteID=www&sitelang="
)
_PAGE_SIZE = 20
# ~7 pages today (130 products); hitting this means the paging stopped working
_MAX_PAGES = 30


def _fetch(url: str) -> str:
    """One plain HTTP GET (sync: callers run it through asyncio.to_thread)."""
    page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True, timeout=30)
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status} from {url}")
    return page.body.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _normalise_product_url(url: str) -> str:
    """Absolute, no query/fragment, no /techspec/ or /spec/, trailing slash."""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.asus.com" + url
    url = url.split("#")[0].split("?")[0]
    url = re.sub(r"/(?:tech)?spec/?$", "/", url)
    return url if url.endswith("/") else url + "/"


def parse_series_page(body: str) -> list[dict]:
    """The `ProductList` of one SeriesFilterResult response."""
    data = json.loads(body)
    result = data.get("Result") if isinstance(data, dict) else None
    products = result.get("ProductList") if isinstance(result, dict) else None
    if not isinstance(products, list):
        raise ValueError("SeriesFilterResult response has no Result.ProductList")
    return products


def laptop_urls(products: list[dict]) -> list[str]:
    """Normalised product URLs, Chromebooks dropped, duplicates removed.

    ROG lists one card per SKU, so the same ROG page appears several times.
    """
    urls: list[str] = []
    for product in products:
        raw = (product.get("ProductURL") or "").strip()
        if not raw:
            continue
        if "chromebook" in raw.lower() or "chromebook" in (product.get("Name") or "").lower():
            continue
        url = _normalise_product_url(raw)
        if url not in urls:
            urls.append(url)
    return urls


def crawl_series(fetch_page: Callable[[int], str]) -> list[str]:
    """Page through the listing until a page comes back short."""
    urls: list[str] = []
    for page in range(1, _MAX_PAGES + 1):
        products = parse_series_page(fetch_page(page))
        if page == 1 and not products:
            # An empty first page is an API change, not a quiet day: returning
            # [] would surface as "No URLs found" and look like success
            raise ValueError("SeriesFilterResult page 1 has no products")
        urls += [u for u in laptop_urls(products) if u not in urls]
        if len(products) < _PAGE_SIZE:
            return urls
    raise ValueError(f"SeriesFilterResult still returning full pages after {_MAX_PAGES} pages")


# ---------------------------------------------------------------------------
# www.asus.com spec page
# ---------------------------------------------------------------------------


def parse_asus_techspec(html: str) -> list[dict]:
    """
    Every variant on a www.asus.com /techspec/ page, from its Nuxt 3 payload.

    Returns { status, product_name, specs, image_urls, source_url_suffix } per
    variant. Raises when the page has no Nuxt 3 payload or no variant data, so
    a changed page can never come back as an empty success.
    """
    state = nuxt3_state(html)
    pinia = state.get("pinia") if isinstance(state, dict) else None
    pd_page = pinia.get("pdPageStore") if isinstance(pinia, dict) else None
    if not pd_page:
        raise ValueError("pinia.pdPageStore not found in the page's Nuxt payload")

    # ---- Variants -----------------------------------------------------
    tech_specs = []

    # Check different possible lists of variants
    for key in ["PDTechSpecM2List", "PDTechSpec", "PDTechSpecM2"]:
        tech_specs = (pd_page.get(key) or {}).get("TechSpec", [])
        if tech_specs:
            break

    # Fallback if there is no TechSpec list but a direct SpecList
    if not tech_specs:
        for key in ["PDTechSpecM2", "PDTechSpec"]:
            obj = pd_page.get(key) or {}
            if "SpecList" in obj:
                tech_specs = [obj]  # wrap single variant in list
                break

    if not tech_specs:
        raise ValueError("No variant data found in pdPageStore TechSpec keys.")

    # ---- Marketing name -----------------------------------------------
    base_product_name: str = (
        (pd_page.get("productTabList") or {}).get("ModelName", "")
        or "Unknown Model"
    )

    num_variants = len(tech_specs)
    results: list[dict] = []

    for idx, sku in enumerate(tech_specs):
        spec_list = sku.get("SpecList") or {}
        if not isinstance(spec_list, dict):
            # PDTechSpec's SpecList is a list of {Title, Content}; dict() of
            # that would quietly produce {"Title": "Content"}
            raise ValueError(f"SpecList is a {type(spec_list).__name__}, expected a dict")
        variant_specs: dict = dict(spec_list)

        model_code = sku.get("Name", "").strip()
        product_name = (
            f"{base_product_name} ({model_code})"
            if model_code else base_product_name
        )

        img_link = sku.get("ImageLink", "")
        if img_link.startswith("//"):
            img_link = "https:" + img_link
        image_urls = [img_link] if img_link.startswith("http") else []

        # www.asus.com prices are not used: they come from the product guides
        # (Jack, 2026-10-27), so the page's price lists are never read
        variant_specs["Price"] = "N/A"

        suffix = f"?v={idx + 1}" if num_variants > 1 else ""

        results.append({
            "status": "success",
            "product_name": product_name,
            "specs": variant_specs,
            "image_urls": image_urls,
            "source_url_suffix": suffix,
        })

    return results


# ---------------------------------------------------------------------------
# rog.asus.com spec page (Playwright until step 10 Phase 2a; runs inside the
# worker thread, via run_async_playwright)
# ---------------------------------------------------------------------------


async def _async_scrape_rog_specs(target_url: str, base_url: str) -> list[dict]:
    """
    Scrape one ROG spec page from window.__NUXT__.state.Spec.spec.

    Returns a list of result dicts — one per variant.
    Each dict: { status, product_name, specs, image_urls, source_url_suffix }
    On failure: { status: "failed", error: "..." }
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        try:
            # ROG site is Nuxt.js. Navigate to the main product page and
            # wait for the Nuxt state store to be populated.
            await page.goto(target_url, wait_until="networkidle", timeout=60000)

            spec_obj: list[dict] = await page.evaluate(
                "() => window.__NUXT__?.state?.Spec?.spec || []"
            )

            if not spec_obj:
                raise Exception(
                    "window.__NUXT__.state.Spec.spec is empty or not found. "
                    "The ROG page may not have loaded fully."
                )

            num_variants = len(spec_obj)
            print(f"🔍 ROG __NUXT__: {num_variants} variant(s) at {base_url}")

            results: list[dict] = []

            for idx, sku in enumerate(spec_obj):
                # --------------------------------------------------------
                # Specs — specContent[].displayField / descriptionText
                # --------------------------------------------------------
                specs: dict = {}
                for item in sku.get("specContent", []):
                    field = item.get("displayField", "").strip()
                    value = item.get("descriptionText", "").strip()
                    if field and value:
                        # descriptionText uses literal \n — convert to readable
                        specs[field] = value.replace("\\n", "\n").strip()

                # --------------------------------------------------------
                # Price — stored as float e.g. 9099.0  →  "RM9,099.00"
                # --------------------------------------------------------
                raw_price = sku.get("price")
                try:
                    price_val = float(raw_price) if raw_price is not None else 0
                    price_str = f"RM{price_val:,.2f}" if price_val > 0 else "N/A"
                except (TypeError, ValueError):
                    price_str = "N/A"
                specs["Price"] = price_str

                # --------------------------------------------------------
                # Product name — mktName + skuName
                # --------------------------------------------------------
                mkt_name = sku.get("mktName", "").strip()
                sku_name = sku.get("skuName", "").strip()
                product_name = f"{mkt_name} ({sku_name})" if sku_name else mkt_name

                # --------------------------------------------------------
                # Image — skuImg is the per-variant hero image
                # --------------------------------------------------------
                sku_img = sku.get("skuImg", "")
                image_urls = [sku_img] if sku_img and sku_img.startswith("http") else []

                # --------------------------------------------------------
                # Source URL suffix — unique key per variant in DB
                # --------------------------------------------------------
                suffix = f"?v={idx + 1}" if num_variants > 1 else ""

                results.append({
                    "status": "success",
                    "product_name": product_name,
                    "specs": specs,
                    "image_urls": image_urls,
                    "source_url_suffix": suffix,
                })

            await browser.close()
            return results

        except Exception as e:
            await browser.close()
            return [{"status": "failed", "error": str(e)}]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def crawl_asus_specs_links(start_url: str = "") -> list[str]:
    """
    Every ASUS + ROG laptop page URL from the SeriesFilterResult listing API.

    *start_url* (the brand's base_scrape_url) is accepted for the common
    crawler signature but not used: the API URL is fixed in this module.
    """
    def fetch_page(page: int) -> str:
        return _fetch(_SERIES_API.format(page=page, size=_PAGE_SIZE))

    return await asyncio.to_thread(crawl_series, fetch_page)


async def scrape_asus_laptop_specs(url: str, brand_id) -> list[dict]:
    """
    Scrape one ASUS or ROG product page; one result dict per variant.
    Each dict: { status, product_name, raw_prices_list, image_urls,
                 raw_specs, source_url_suffix }, or { status: "failed", error }.
    """
    clean_url = url
    for suffix in ["/techspec/", "/techspec", "/spec/", "/spec"]:
        if clean_url.endswith(suffix):
            clean_url = clean_url[:-len(suffix)]
            break
    base_url = clean_url if clean_url.endswith("/") else clean_url + "/"
    is_rog = "rog.asus.com" in clean_url

    # The spec/techspec subpage holds the full Nuxt spec data
    if is_rog:
        raw_list: list[dict] = await run_async_playwright(
            _async_scrape_rog_specs(base_url + "spec/", base_url)
        )
    else:
        try:
            html = await asyncio.to_thread(_fetch, base_url + "techspec/")
            raw_list = parse_asus_techspec(html)
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
