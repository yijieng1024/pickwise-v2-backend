import time
from typing import Optional
from urllib.parse import quote_plus

import httpx
from langchain_core.tools import tool
from rapidfuzz import fuzz

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

# parse.bot iPrice Malaysia API (aggregates Shopee/Lazada/etc. listings).
# Free tier: 100 credits/month, 5 req/min — hence the aggressive cache below.
_API_URL = "https://api.parse.bot/scraper/9a07fc78-2b92-4504-b960-fe70fa18cbaa/search_products"
_TIMEOUT_S = 20.0
_MAX_LISTINGS = 5
# Listings whose title doesn't resemble the query are dropped (accessories,
# sleeves, chargers etc. show up in marketplace keyword search).
_RELEVANCE_THRESHOLD = 60

# query → (fetched_at, response dict). Prices move slowly; credits don't.
_CACHE_TTL_S = 6 * 3600
_cache: dict[str, tuple[float, dict]] = {}


def _marketplace_links(query: str) -> dict:
    encoded = quote_plus(query)
    return {
        "shopee_search_url": f"https://shopee.com.my/search?keyword={encoded}",
        "lazada_search_url": f"https://www.lazada.com.my/catalog/?q={encoded}",
    }


def _fallback(query: str, note: str) -> dict:
    return {
        "status": "fallback_links",
        "query": query,
        "listings": [],
        **_marketplace_links(query),
        "note": note + " Share the marketplace search links so the user can check live listings themselves.",
    }


def _parse_price(raw) -> Optional[float]:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        cleaned = raw.replace("RM", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _to_listing(item: dict, query: str) -> Optional[dict]:
    title = item.get("title") or ""
    price = _parse_price(item.get("price"))
    if not title or price is None or price <= 0:
        return None
    relevance = fuzz.token_set_ratio(query.lower(), title.lower())
    if relevance < _RELEVANCE_THRESHOLD:
        return None
    store = item.get("store") or {}
    shop = item.get("shop") or {}
    return {
        "title": title,
        "price_rm": price,
        "store": store.get("name") or shop.get("name"),
        "relevance": round(relevance, 1),
    }


@tool
def search_malaysian_market_price(product_name: str, model_code: Optional[str] = None) -> dict:
    """
    Look up live Malaysian market prices for a laptop across local retailers
    (Shopee, Lazada, etc.) via the iPrice Malaysia aggregator.

    Args:
        product_name: Full laptop name (e.g. "ASUS ROG Strix G16").
        model_code: Optional model code for a more precise search.

    Returns:
        A dict with:
        - status: "ok" when live listings were found; "no_match" when the
          search returned nothing relevant; "fallback_links" when the live
          lookup is unavailable (no API key / API error).
        - listings: up to 5 matching listings, each with title, price_rm,
          store, and relevance (0-100 title match vs the query).
        - price_min_rm / price_max_rm: range across the returned listings
          (only when status is "ok").
        - shopee_search_url / lazada_search_url: direct marketplace search
          links — always included, offer them to the user for verification.
        - note: caveats to relay (e.g. aggregator data may lag live prices).

        Always cite prices as indicative market prices, mention the store
        name, and note that listings should be verified before purchase.
    """
    query = " ".join(part for part in [product_name, model_code] if part).strip()

    if not settings.parsebot_api_key:
        return _fallback(
            query,
            "Live market price lookup is not configured (PARSEBOT_API_KEY missing).",
        )

    cache_key = query.lower()
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]

    try:
        resp = httpx.get(
            _API_URL,
            params={"query": query, "page": 1},
            headers={"X-API-Key": settings.parsebot_api_key},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("market_price: iPrice API call failed for %r: %s", query, e)
        return _fallback(query, f"Live market price lookup failed ({type(e).__name__}).")

    if not isinstance(payload, dict) or payload.get("error"):
        logger.warning("market_price: iPrice API error payload for %r: %.300s", query, payload)
        return _fallback(query, "Live market price lookup failed (upstream error).")

    # Listings documented at top level, but observed nested under `data` too.
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = data.get("list") or []
    listings = [l for l in (_to_listing(item, query) for item in items) if l]
    listings.sort(key=lambda l: (-l["relevance"], l["price_rm"]))
    listings = listings[:_MAX_LISTINGS]

    if not listings:
        result = {
            "status": "no_match",
            "query": query,
            "listings": [],
            **_marketplace_links(query),
            "note": (
                "No relevant listings found on the iPrice Malaysia aggregator. "
                "Tell the user honestly that no live price was found; offer the "
                "marketplace search links instead."
            ),
        }
    else:
        prices = [l["price_rm"] for l in listings]
        result = {
            "status": "ok",
            "query": query,
            "listings": listings,
            "price_min_rm": min(prices),
            "price_max_rm": max(prices),
            **_marketplace_links(query),
            "note": (
                "Prices are aggregated from Malaysian marketplaces and may lag "
                "live listings — present them as indicative and suggest "
                "verifying on the store page before buying."
            ),
        }

    # Only cache successful lookups — a transient upstream failure or empty
    # result must not be pinned for the full TTL.
    if result["status"] == "ok":
        _cache[cache_key] = (time.monotonic(), result)
    return result
