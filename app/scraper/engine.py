from playwright.sync_api import sync_playwright
from typing import Dict, Any
import re
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

def extract_apple_specs(page) -> dict:
    title = page.title()
    clean_price = 0.0
    
    try:
        price_element = page.wait_for_selector('.rc-prices-fullprice, .violator-frameless', timeout=5000)
        
        if price_element:
            raw_price_text = price_element.inner_text()
            
            # --- THE REGEX PARSER ---
            # 1. Strip commas first so "6,999.00" becomes "6999.00"
            text_without_commas = raw_price_text.replace(',', '')
            
            # 2. Extract all standalone numbers (including decimals) from the text
            # This turns "From 2499 or 104.13/mo" into a clean list: ['2499', '104.13']
            found_numbers = re.findall(r'\b\d+(?:\.\d+)?\b', text_without_commas)
            
            if found_numbers:
                # Apple always lists the full price first, so we grab index [0]
                clean_price = float(found_numbers[0])
                
    except PlaywrightTimeoutError:
        print(f"⚠️ Could not locate price for {title}. Defaulting to 0.0")
        pass

    # Extract the clean product name
    product_name = title.split('-')[0].replace('Buy', '').strip()

    return {
        "brand": "Apple",
        "product_name": product_name,
        "price_rm": clean_price,
        "status": "success" if clean_price > 0 else "partial_success"
    }

def extract_lenovo_specs(page) -> Dict[str, Any]:
    title = page.title()
    price_element = page.wait_for_selector('.saleprice', timeout=5000)
    price_text = price_element.inner_text() if price_element else "0"
    clean_price = float(price_text.replace('RM', '').replace(',', '').strip()) if price_text != "0" else 0.0

    return {
        "brand": "Lenovo",
        "product_name": title,
        "price_rm": clean_price,
        "status": "success"
    }

def scrape_official_website(url: str, brand: str) -> Dict[str, Any]:
    # Notice we use 'with' instead of 'async with'
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
            elif brand.lower() == "lenovo":
                data = extract_lenovo_specs(page)
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