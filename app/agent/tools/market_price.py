from typing import Optional
from urllib.parse import quote_plus

from langchain_core.tools import tool


@tool
def search_malaysian_market_price(product_name: str, model_code: Optional[str] = None) -> dict:
    """
    Find where to buy a laptop in the Malaysian market (Shopee, Lazada).

    NOT YET IMPLEMENTED as a live price search — returns direct marketplace
    search links the user can open to check current listings and pricing
    themselves.

    Args:
        product_name: Full laptop name (e.g. "ASUS ROG Strix G16").
        model_code: Optional model code for a more precise search.
    """
    query = " ".join(part for part in [product_name, model_code] if part).strip()
    encoded = quote_plus(query)

    return {
        "status": "not_implemented",
        "product_name": product_name,
        "shopee_search_url": f"https://shopee.com.my/search?keyword={encoded}",
        "lazada_search_url": f"https://www.lazada.com.my/catalog/?q={encoded}",
        "note": (
            "Live marketplace price scraping is not implemented yet. "
            "These are direct search links on Shopee and Lazada for the user to check."
        ),
    }
