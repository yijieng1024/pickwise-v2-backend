from playwright.async_api import async_playwright

from .playwright_utils import run_async_playwright


# ---------------------------------------------------------------------------
# Internal async implementations (run inside the worker thread, via
# run_async_playwright)
# ---------------------------------------------------------------------------


async def _async_crawl_asus_specs_links(start_url: str) -> list[str]:
    laptop_urls = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        await page.goto(start_url, wait_until="domcontentloaded")

        # Scroll to the bottom of the page to trigger lazy loading
        previous_height = await page.evaluate("document.body.scrollHeight")
        while True:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)  # Wait for 2 seconds
            current_height = await page.evaluate("document.body.scrollHeight")

            if current_height == previous_height:
                break
            previous_height = current_height

        # Extract all laptop URLs
        learn_more_buttons = await page.locator("a[aria-label*='LearnMore']").all()

        for btn in learn_more_buttons:
            href = await btn.get_attribute("href")
            if href:
                if not href.startswith("http"):
                    href = "https://www.asus.com" + href
                laptop_urls.append(href)

        await browser.close()

    return laptop_urls


async def _async_scrape_asus_laptop_specs(url: str) -> list[dict]:
    """
    Scrape one ASUS/ROG spec page.

    ROG pages  → extract from __NEXT_DATA__ JSON (zero CSS selectors, variant-aware)
    ASUS pages → DOM scraping via stable [class*="..."] partial selectors

    Returns a list of result dicts — one per variant.
    Each dict: { status, product_name, specs, image_urls, source_url_suffix }
    On failure: { status: "failed", error: "..." }
    """
    clean_url = url
    for suffix in ["/techspec/", "/techspec", "/spec/", "/spec"]:
        if clean_url.endswith(suffix):
            clean_url = clean_url[:-len(suffix)]
            break
    base_url = clean_url if clean_url.endswith("/") else clean_url + "/"
    is_rog = "rog.asus.com" in clean_url
    
    # We MUST visit the spec/techspec subpage to get the full NUXT spec data
    target_url = base_url + "spec/" if is_rog else base_url + "techspec/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        try:
            # ================================================================
            # ROG branch — window.__NUXT__.state.Spec.spec extraction
            # ================================================================
            if is_rog:
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

            # ================================================================
            # Standard ASUS branch — window.__NUXT__ JSON extraction
            # ================================================================
            else:
                # ASUS standard site is also Nuxt.js.
                # All variant data lives in __NUXT__.state.PDPage.
                await page.goto(target_url, wait_until="networkidle", timeout=60000)

                pd_page: dict = await page.evaluate(
                    "() => window.__NUXT__?.state?.PDPage || {}"
                )

                if not pd_page:
                    raise Exception(
                        "window.__NUXT__.state.PDPage not found. "
                        "The ASUS page may not have loaded fully."
                    )

                # ---- Variants -------------------------------------------------
                tech_specs = []
                
                # Check different possible lists of variants
                for key in ["PDTechSpecM2List", "PDTechSpec", "PDTechSpecM2"]:
                    tech_specs = pd_page.get(key, {}).get("TechSpec", [])
                    if tech_specs:
                        break
                        
                # Fallback if there is no TechSpec list but a direct SpecList
                if not tech_specs:
                    for key in ["PDTechSpecM2", "PDTechSpec"]:
                        obj = pd_page.get(key, {})
                        if "SpecList" in obj:
                            tech_specs = [obj]  # wrap single variant in list
                            break

                if not tech_specs:
                    raise Exception(f"No variant data found in PDPage TechSpec keys at {target_url}.")

                # ---- Marketing name -------------------------------------------
                base_product_name: str = (
                    pd_page.get("productTabList", {}).get("ModelName", "")
                    or "Unknown Model"
                )

                # ---- Price lookup ---------------------------------------------
                price_map: dict[str, str] = {}
                fallback_price = None
                valid_prices_found = 0
                
                # Combine all possible price lists
                all_price_lists = []
                for key in ["PDPriceList", "modelPrice", "modelSkuPrice"]:
                    all_price_lists.extend(pd_page.get(key, {}).get("ProductList", []))
                    
                for p_entry in all_price_lists:
                    # Try all possible ID fields
                    pid = str(p_entry.get("ProductID") or p_entry.get("ProductId") or p_entry.get("PartNo") or p_entry.get("skuId") or "")
                    
                    price_val = p_entry.get("Price")
                    if not price_val and price_val != 0:
                        price_val = p_entry.get("SpecialPrice")
                        
                    if price_val and price_val != "N/A":
                        valid_prices_found += 1
                        if not fallback_price:
                            fallback_price = str(price_val)  # store the first valid price
                        if pid:
                            price_map[pid] = str(price_val)

                num_variants = len(tech_specs)
                print(f"🔍 ASUS __NUXT__: {num_variants} variant(s). Price Map: {price_map}")

                asus_results: list[dict] = []

                for idx, sku in enumerate(tech_specs):
                    variant_specs: dict = dict(sku.get("SpecList", {}))

                    model_code = sku.get("Name", "").strip()
                    product_name = (
                        f"{base_product_name} ({model_code})"
                        if model_code else base_product_name
                    )

                    img_link = sku.get("ImageLink", "")
                    if img_link.startswith("//"):
                        img_link = "https:" + img_link
                    image_urls = [img_link] if img_link.startswith("http") else []

                    # 1. Try to match by ID
                    product_id = str(sku.get("ProductId") or sku.get("ProductID") or sku.get("PartNo") or "")
                    price = price_map.get(product_id)
                    
                    # 2. Fallback to the single valid page price if ID map fails
                    if not price and fallback_price and valid_prices_found == 1:
                        price = fallback_price
                        
                    variant_specs["Price"] = price or "N/A"

                    suffix = f"?v={idx + 1}" if num_variants > 1 else ""

                    asus_results.append({
                        "status": "success",
                        "product_name": product_name,
                        "specs": variant_specs,
                        "image_urls": image_urls,
                        "source_url_suffix": suffix,
                    })

                await browser.close()
                return asus_results

        except Exception as e:
            await browser.close()
            return [{"status": "failed", "error": str(e)}]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def crawl_asus_specs_links(start_url: str) -> list[str]:
    """
    Navigates to the ASUS listing page, scrolls to the bottom,
    and returns a list of all laptop 'Learn More' URLs.
    Offloads Playwright to a worker thread with its own event loop so
    it works correctly when uvicorn uses SelectorEventLoop on Windows.
    """
    return await run_async_playwright(_async_crawl_asus_specs_links(start_url))


async def scrape_asus_laptop_specs(url: str, brand_id) -> list[dict]:
    """
    Navigates to the specific ASUS product page and extracts all variants
    from window.__NUXT__ state — no CSS selectors.
    Returns a list of formatted result dicts — one per variant found.
    Each dict: { status, product_name, raw_prices_list, image_urls,
                 raw_specs, source_url_suffix }
    Offloads Playwright to a worker thread with its own event loop so
    it works correctly when uvicorn uses SelectorEventLoop on Windows.
    """
    raw_list: list[dict] = await run_async_playwright(
        _async_scrape_asus_laptop_specs(url)
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
