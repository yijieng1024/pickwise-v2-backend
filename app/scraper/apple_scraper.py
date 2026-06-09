from playwright.sync_api import sync_playwright
from typing import Dict, Any
import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def crawl_apple_specs_links(start_url: str = "https://www.apple.com/my/mac/") -> list:
    print(f"🕸️ Crawler booting up... Scanning on '{start_url}'")

    unique_spec_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto(start_url, wait_until="networkidle", timeout=15000)

            learn_more_hrefs = page.eval_on_selector_all(
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
            browser.close()

    final_list = list(unique_spec_links)
    print(
        f"✅ Crawler completed! Successfully computed {len(final_list)} unique Tech Specs links."
    )
    return final_list

def extract_apple_specs(page) -> dict:
    title = page.title()
    product_name = (
        title.split("-")[0].replace("Buy", "").replace("Tech Specs", "").strip()
    )

    raw_prices_list = []
    raw_specs_list = []
    extracted_images = []

    try:
        meta_image = page.locator('meta[property="og:image"]').first
        if meta_image:
            extracted_images.append(meta_image.get_attribute("content"))

        all_imgs = page.locator("img").evaluate_all("imgs => imgs.map(i => i.src)")
        for src in all_imgs:
            if not src:
                continue
            src_lower = src.lower()
            if (
                any(ext in src_lower for ext in [".png", ".jpg", ".jpeg", ".webp"])
                and "icon" not in src_lower
                and "logo" not in src_lower
            ):
                if src not in extracted_images:
                    extracted_images.append(src)
    except Exception as e:
        print(f"⚠️ Image extraction error: {e}")

    size_tabs = ["13-inch", "14-inch", "15-inch", "16-inch"]

    tabs_found_on_page = []
    for size in size_tabs:
        tab_locator = page.locator(
            f'button:has-text("{size}"), a:has-text("{size}")'
        ).first
        if tab_locator.is_visible():
            tabs_found_on_page.append({"size_name": size, "locator": tab_locator})

    if not tabs_found_on_page:
        tabs_found_on_page = [{"size_name": "Default", "locator": None}]

    for tab in tabs_found_on_page:
        size_label = tab["size_name"]

        if tab["locator"]:
            print(f"Selecting size: {size_label}...")
            tab["locator"].click()
            page.wait_for_timeout(1000)

        try:
            price_row = (
                page.locator(".techspecs-row")
                .filter(has=page.locator(".techspecs-rowheader", has_text="Price"))
                .first
            )
            if price_row.count() > 0:
                price_columns = price_row.locator(".techspecs-column").all()
                for col in price_columns:
                    col_text = col.inner_text()
                    clean_text = " | ".join(
                        [
                            line.strip().replace("\xa0", " ")
                            for line in col_text.split("\n")
                            if line.strip()
                        ]
                    )
                    if clean_text:
                        # 💡 Hints: Price label LangGraph loves
                        tagged_price = f"[{size_label}] {clean_text}"
                        if tagged_price not in raw_prices_list:
                            raw_prices_list.append(tagged_price)
        except Exception:
            pass

        try:
            subheaders = page.locator(".techspecs-subheader").all_inner_texts()
            for text in subheaders:
                clean_text = text.strip().replace("\xa0", " ")
                if clean_text:
                    tagged_spec = f"[{size_label}] {clean_text}"
                    if tagged_spec not in raw_specs_list:
                        raw_specs_list.append(tagged_spec)

            list_items = page.locator(".techspecs-list li").all_inner_texts()
            for text in list_items:
                clean_text = text.strip().replace("\xa0", " ")
                if clean_text:
                    tagged_spec = f"[{size_label}] {clean_text}"
                    if tagged_spec not in raw_specs_list:
                        raw_specs_list.append(tagged_spec)
        except Exception:
            pass

    return {
        "brand": "Apple",
        "product_name": product_name,
        "raw_prices_list": raw_prices_list,
        "image_urls": extracted_images,
        "raw_specs": raw_specs_list,
        "status": "success" if len(raw_specs_list) > 0 else "failed",
    }


def scrape_official_website(
    url: str, brand_name: str, brand_id: str
) -> Dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=20000)

            if brand_name.lower() == "apple":
                data = extract_apple_specs(page)
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
            browser.close()
