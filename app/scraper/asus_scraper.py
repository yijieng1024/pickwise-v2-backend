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
        page = await browser.new_page()

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


async def _async_scrape_asus_laptop_specs(url: str) -> dict:
    base_url = url if url.endswith('/') else url + '/'
    is_rog = "rog.asus.com" in url
    tech_spec_url = base_url + "spec/" if is_rog else base_url + "techspec/"

    specs = {}
    image_urls = []
    product_name = "Unknown Model"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            await page.goto(tech_spec_url, wait_until="domcontentloaded", timeout=30000)
            # Wait 10 seconds for the full page (including JS-rendered price) to load.
            await page.wait_for_timeout(10000)

            # --- Locators ---
            if is_rog:
                target_row_class = ".ProductSpec__row__wSwCC"
                target_title_class = ".ProductSpec__productSpecItemTitle__JVvSd"
                target_value_class = ".ProductSpec__rowItem__hGYWS"
                target_price_class = ".ProductSpecPrice__finallyPriceValue__fUbBJ"
            else:
                target_row_class = ".TechSpec__rowTable__1LR9D"
                target_title_class = ".rowTableTitle"
                target_value_class = ".TechSpec__rowTableItems__KYWXp"
                target_name_class = ".LevelFourProductPageHeader__modelName__70ttK"

            # --- Extract Model Name ---
            # Scraped from: <h1 class="LevelFourProductPageHeader__modelName__70ttK">ASUS Vivobook 14 (A1407)</h1>
            if not is_rog:
                model_locator = page.locator(target_name_class)  # type: ignore
                if await model_locator.count() > 0:
                    name_text = await model_locator.first.text_content()
                    product_name = name_text.strip() if name_text else "Unknown Model"

            # --- Extract Product Images ---
            if not is_rog:
                image_elements = await page.locator(".TechSpec__rowImage__35vd6 img").all()
                for img_el in image_elements:
                    src = await img_el.get_attribute("src")
                    if src:
                        # Normalise protocol-relative URLs to absolute HTTPS
                        if src.startswith("//"):
                            src = "https:" + src
                        image_urls.append(src)

            # --- Extract Specs (text rows only) ---
            rows = await page.locator(target_row_class).all()

            if len(rows) == 0:
                raise Exception(f"No tech specs were found using locator: {target_row_class}")

            for row in rows:
                title_locator = row.locator(target_title_class)
                value_locator = row.locator(target_value_class)

                if await title_locator.count() > 0 and await value_locator.count() > 0:
                    title = await title_locator.first.text_content()
                    title = title.strip() if title else "Unknown"

                    # Skip dedicated image rows — already captured above in image_urls
                    is_image_row = await value_locator.locator(".TechSpec__rowImage__35vd6").count() > 0
                    if is_image_row:
                        continue

                    img_locator = value_locator.locator("img")
                    if await img_locator.count() > 0:
                        value = await img_locator.first.get_attribute("src")
                    else:
                        value = await value_locator.first.text_content()

                    if value:
                        specs[title] = value.strip().replace('\n', ', ')

            # --- Extract Price (scraped last) ---
            # Price is in a sticky header that only appears after scrolling.
            # We scrape it last so the page has had maximum time to fully render.
            price_selectors = [
                ".LevelFourProductPageHeader__priceNoDiscount__3Ayb4",  # full price (no discount)
                ".LevelFourProductPageHeader__price__3qU_7",            # discounted price
            ]
            await page.wait_for_timeout(1000)               # wait for animation

            specs["Price"] = "N/A"
            for price_sel in price_selectors:
                price_el = page.locator(price_sel).first
                if await price_el.count() > 0:
                    price_text = await price_el.inner_text()
                    if not price_text or not price_text.strip():
                        price_text = await price_el.text_content()
                    if price_text and price_text.strip():
                        specs["Price"] = price_text.strip()
                        break

            # Fallback: business laptops (e.g. ExpertBook) don't show price on
            # the techspec subpage — open the main product page and try there.
            if specs["Price"] == "N/A":
                main_page = await browser.new_page()
                await main_page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                await main_page.wait_for_timeout(10000)
                await main_page.evaluate("window.scrollTo(0, 500)")
                await main_page.wait_for_timeout(1000)
                for price_sel in price_selectors:
                    price_el = main_page.locator(price_sel).first
                    if await price_el.count() > 0:
                        price_text = await price_el.inner_text()
                        if not price_text or not price_text.strip():
                            price_text = await price_el.text_content()
                        if price_text and price_text.strip():
                            specs["Price"] = price_text.strip()
                            break
                await main_page.close()

        except Exception as e:
            await browser.close()
            return {"status": "failed", "error": str(e)}

        await browser.close()

    # Return structured result with specs, image_urls, and product_name separated
    return {"specs": specs, "image_urls": image_urls, "product_name": product_name}


# ---------------------------------------------------------------------------
# Public entry points — call these from main.py / your route handlers.
# Same call shape as apple_scraper's crawl_apple_specs_links /
# scrape_official_website, so main.py can treat every brand scraper
# identically.
# ---------------------------------------------------------------------------

async def crawl_asus_specs_links(start_url: str) -> list[str]:
    """
    Navigates to the ASUS listing page, scrolls to the bottom,
    and returns a list of all laptop 'Learn More' URLs.
    Offloads Playwright to a worker thread with its own event loop so
    it works correctly when uvicorn uses SelectorEventLoop on Windows.
    """
    return await run_async_playwright(_async_crawl_asus_specs_links(start_url))


async def scrape_asus_laptop_specs(url: str, brand_id) -> dict:
    """
    Navigates to the specific ASUS /techspec/ or ROG /spec/ page and extracts the data.
    Offloads Playwright to a worker thread with its own event loop so
    it works correctly when uvicorn uses SelectorEventLoop on Windows.
    """
    raw = await run_async_playwright(_async_scrape_asus_laptop_specs(url))

    if raw.get("status") == "failed":
        return raw

    raw_specs = raw.get("specs", {})
    image_urls = raw.get("image_urls", [])
    product_name = raw.get("product_name", "Unknown Model")

    return {
        "status": "success",
        "product_name": product_name,
        "raw_prices_list": [{"price": raw_specs.get("Price", "N/A")}],
        "image_urls": image_urls,
        "raw_specs": raw_specs,
    }