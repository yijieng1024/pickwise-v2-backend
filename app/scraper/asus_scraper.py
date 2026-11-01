"""
ASUS Malaysia scraper: www.asus.com and rog.asus.com, one brand (`asus`).

- Discovery: ASUS's own listing API (`SeriesFilterResult`), paged, which lists
  www and ROG laptops together. No browser.
- www.asus.com spec pages (Nuxt 3): fetched with Scrapling's `Fetcher` and read
  from the `__NUXT_DATA__` payload (`pinia.pdPageStore`) by `parse_asus_techspec`.
  No price: www prices come only from the product guides (step 10, Phase 3).
- rog.asus.com spec pages (Nuxt 2): fetched the same way and read from the
  `window.__NUXT__` literal (`state.Spec.spec`) by `parse_rog_spec`. Each SKU
  carries its own price; 0 means unknown.
"""
import asyncio
import json
import re
from typing import Callable

from .fetch import fetch_html as _fetch
from .nuxt_state import nuxt2_state, nuxt3_state

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
# rog.asus.com spec page
# ---------------------------------------------------------------------------


def parse_rog_spec(html: str) -> list[dict]:
    """
    Every SKU on a rog.asus.com /spec/ page, from its Nuxt 2 state.

    Returns { status, product_name, specs, image_urls, source_url_suffix } per
    SKU, with the SKU itself in specs["SKU"] for the product guide match.
    Raises when the page has no state or no SKUs, so a changed page can never
    come back as an empty success.
    """
    state = nuxt2_state(html).get("state")
    spec = state.get("Spec") if isinstance(state, dict) else None
    spec_list = spec.get("spec") if isinstance(spec, dict) else None
    if not spec_list:
        raise ValueError("window.__NUXT__.state.Spec.spec is empty or not found")

    num_variants = len(spec_list)
    results: list[dict] = []

    for idx, sku in enumerate(spec_list):
        # Specs: specContent[].displayField / descriptionText
        specs: dict = {}
        for item in sku.get("specContent") or []:
            field = (item.get("displayField") or "").strip()
            value = (item.get("descriptionText") or "").strip()
            if field and value:
                # descriptionText uses literal \\n: convert to readable
                specs[field] = value.replace("\\n", "\n").strip()

        # Price: stored as a number, e.g. 9099.0 becomes "RM9,099.00"; 0 is unknown
        raw_price = sku.get("price")
        try:
            price_val = float(raw_price) if raw_price is not None else 0
            price_str = f"RM{price_val:,.2f}" if price_val > 0 else "N/A"
        except (TypeError, ValueError):
            price_str = "N/A"
        specs["Price"] = price_str

        mkt_name = (sku.get("mktName") or "").strip()
        sku_name = (sku.get("skuName") or "").strip()
        specs["SKU"] = sku_name
        product_name = f"{mkt_name} ({sku_name})" if sku_name else mkt_name

        # skuImg is the per-SKU hero image
        sku_img = sku.get("skuImg") or ""
        image_urls = [sku_img] if sku_img.startswith("http") else []

        suffix = f"?v={idx + 1}" if num_variants > 1 else ""

        results.append({
            "status": "success",
            "product_name": product_name,
            "specs": specs,
            "image_urls": image_urls,
            "source_url_suffix": suffix,
        })

    return results


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
    page, parse = (
        (base_url + "spec/", parse_rog_spec) if is_rog else (base_url + "techspec/", parse_asus_techspec)
    )
    try:
        raw_list: list[dict] = parse(await asyncio.to_thread(_fetch, page))
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
