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
    product_name = title.split("-")[0].replace("Buy", "").replace("Tech Specs", "").strip()
    
    extracted_images = []
    try:
        meta_image = page.locator('meta[property="og:image"]').first
        if meta_image:
            extracted_images.append(meta_image.get_attribute("content"))
        all_imgs = page.locator("img").evaluate_all("imgs => imgs.map(i => i.src)")
        for src in all_imgs:
            if not src: continue
            src_lower = src.lower()
            if any(ext in src_lower for ext in [".png", ".jpg", ".jpeg", ".webp"]) and "icon" not in src_lower and "logo" not in src_lower:
                if src not in extracted_images:
                    extracted_images.append(src)
    except Exception as e:
        print(f"⚠️ Image error: {e}")

    raw_specs_text = ""
    try:
        page.wait_for_selector(".techspecs-section", timeout=5000)
        sections_texts = page.locator(".techspecs-section").all_inner_texts()
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
