from playwright.sync_api import sync_playwright
from typing import Dict, Any
import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

def extract_apple_specs(page) -> dict:
    title = page.title()
    clean_price = 0.0
    image_url = None
    raw_specs_list = []
    
    # 1. PRICE EXTRACTION
    try:
        price_element = page.wait_for_selector('.rc-prices-fullprice, .violator-frameless', timeout=5000)
        if price_element:
            raw_price_text = price_element.inner_text()
            text_without_commas = raw_price_text.replace(',', '')
            found_numbers = re.findall(r'\b\d+(?:\.\d+)?\b', text_without_commas)
            
            if found_numbers:
                clean_price = float(found_numbers[0])
    except PlaywrightTimeoutError:
        print(f"⚠️ Timeout: Could not locate price for {title}.")

    # 2. IMAGE EXTRACTION
    try:
        meta_image = page.locator('meta[property="og:image"]').first
        if meta_image:
            image_url = meta_image.get_attribute('content')
    except Exception as e:
        print(f"⚠️ Could not locate meta image: {e}")

    # 3. HARDWARE SPECS EXTRACTION
    try:
        page.wait_for_selector('ul li', timeout=3000)
        
        list_items = page.query_selector_all('li')
        for item in list_items:
            text = item.inner_text().strip()
            if text and any(keyword in text for keyword in ['-core', 'GB', 'TB', 'Unified Memory', 'SSD']):
                # Prevent duplicates
                if text not in raw_specs_list:
                    raw_specs_list.append(text)
    except PlaywrightTimeoutError:
        print(f"⚠️ Timeout: Could not locate hardware specs list.")

    product_name = title.split('-')[0].replace('Buy', '').strip()

    return {
        "brand": "Apple",
        "product_name": product_name,
        "price_rm": clean_price,
        "image_url": image_url,
        "raw_specs": raw_specs_list,
        "status": "success" if clean_price > 0 else "partial_success"
    }

def scrape_official_website(url: str, brand: str) -> Dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=20000)

            if brand.lower() == "apple":
                data = extract_apple_specs(page)
            else:
                raise ValueError(f"No extraction logic built for brand: {brand}")
            
            data["source_url"] = url
            return data

        except Exception as e:
            return {
                "brand": brand,
                "source_url": url,
                "status": "failed",
                "error": str(e)
            }
        finally:
            browser.close()