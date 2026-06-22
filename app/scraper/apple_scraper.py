from typing import Dict, Any

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .playwright_utils import run_async_playwright


# ---------------------------------------------------------------------------
# Internal async implementations (run inside the worker thread, via
# run_async_playwright). Same shape as asus_scraper's _async_* functions.
# ---------------------------------------------------------------------------

async def _async_crawl_apple_specs_links(start_url: str) -> list:
    print(f"🕸️ Crawler booting up... Scanning on '{start_url}'")

    unique_spec_links = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            await page.goto(start_url, wait_until="networkidle", timeout=15000)

            learn_more_hrefs = await page.eval_on_selector_all(
                "a",
                """elements => {
                return elements
                    .filter(e => e.innerText && e.innerText.toLowerCase().includes('learn more'))
                    .map(e => e.href);
            }""",
            )

            print(f"👁️ Crawler found {len(learn_more_hrefs)} 'Learn more' buttons.")

            for href in learn_more_hrefs:
                if not href:
                    continue

                clean_url = href.split("?")[0].rstrip("/")

                blacklist = ["mac-does-that", "accessories", "displays"]
                if any(bad_word in clean_url for bad_word in blacklist):
                    continue

                # Process only Mac-related links
                if "/my/mac" in clean_url or "/my/imac" in clean_url:
                    # The Magic Step: Pre-calculate the specs URL without loading the middle page
                    if not clean_url.endswith("specs"):
                        spec_url = f"{clean_url}/specs/"
                        unique_spec_links.add(spec_url)
                    else:
                        unique_spec_links.add(clean_url)

        except Exception as e:
            print(f"⚠️ Crawler encountered an error: {e}")

        finally:
            await browser.close()

    final_list = list(unique_spec_links)
    print(
        f"✅ Crawler completed! Successfully computed {len(final_list)} unique Tech Specs links."
    )
    return final_list


async def _extract_apple_specs(page) -> dict:
    title = await page.title()
    product_name = title.split("-")[0].replace("Buy", "").replace("Tech Specs", "").strip()

    extracted_images = []
    try:
        meta_image = page.locator('meta[property="og:image"]').first
        if await meta_image.count() > 0:
            content = await meta_image.get_attribute("content")
            if content:
                extracted_images.append(content)

        all_imgs = await page.locator("img").evaluate_all("imgs => imgs.map(i => i.src)")
        for src in all_imgs:
            if not src:
                continue
            src_lower = src.lower()
            if any(ext in src_lower for ext in [".png", ".jpg", ".jpeg", ".webp"]) and "icon" not in src_lower and "logo" not in src_lower:
                if src not in extracted_images:
                    extracted_images.append(src)
    except Exception as e:
        print(f"⚠️ Image error: {e}")

    raw_specs_text = ""
    try:
        await page.wait_for_selector(".techspecs-section", timeout=5000)
        sections_texts = await page.locator(".techspecs-section").all_inner_texts()
        raw_specs_text = "\n\n".join([text.strip() for text in sections_texts if text.strip()])
    except PlaywrightTimeoutError:
        print(f"⚠️ Timeout: .techspecs-section not found on {title}.")

    return {
        "brand": "Apple",
        "product_name": product_name,
        "raw_prices_list": [],
        "image_urls": extracted_images,
        "raw_specs": [raw_specs_text],
        "status": "success" if raw_specs_text else "failed",
    }


async def _async_scrape_official_website(
    url: str, brand_name: str, brand_id: str
) -> Dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)

            if brand_name.lower() == "apple":
                data = await _extract_apple_specs(page)
            else:
                raise ValueError(f"No extraction logic built for brand: {brand_name}")

            data["source_url"] = url
            if brand_id:
                data["brand_id"] = brand_id
            return data

        except Exception as e:
            return {
                "brand": brand_name,
                "source_url": url,
                "status": "failed",
                "error": str(e),
                "brand_id": brand_id,
            }
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Public entry points — call these from main.py / your route handlers.
# Both are now `async def`, with the exact same call shape as
# asus_scraper.crawl_asus_specs_links / scrape_asus_laptop_specs, so
# main.py can treat every brand scraper identically.
# ---------------------------------------------------------------------------

async def crawl_apple_specs_links(start_url: str = "https://www.apple.com/my/mac/") -> list:
    """
    Crawls the Apple Mac listing page and returns a list of computed
    Tech Specs page URLs.
    Offloads Playwright to a worker thread with its own event loop so
    it works correctly when uvicorn uses SelectorEventLoop on Windows.
    """
    return await run_async_playwright(_async_crawl_apple_specs_links(start_url))


async def scrape_official_website(
    url: str, brand_name: str, brand_id: str
) -> Dict[str, Any]:
    """
    Navigates to a specific product's official spec page and extracts data.
    Offloads Playwright to a worker thread with its own event loop so
    it works correctly when uvicorn uses SelectorEventLoop on Windows.
    """
    return await run_async_playwright(
        _async_scrape_official_website(url, brand_name, brand_id)
    )