import time
from typing import Optional
from urllib.parse import quote_plus

import httpx
from langchain_core.tools import tool
from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.config import settings
from app.database import engine
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.logger import get_logger

logger = get_logger(__name__)

# Layer 2: SerpApi (serpapi.com) Google Shopping, geo-targeted to Malaysia —
# aggregates Shopee/Lazada/senQ/Harvey Norman/etc. Free tier is only ~100
# searches/MONTH, so the cache below is essential. Layer 1 (own catalog) is
# local and always available.
_SERPAPI_URL = "https://serpapi.com/search"
_TIMEOUT_S = 20.0
_MAX_LISTINGS = 5
# Listings whose title doesn't resemble the query are dropped. Note
# token_set_ratio scores 100 on any title containing all query tokens, so
# accessories titled "<laptop name> skin/battery/..." pass it — hence the
# keyword blocklist and price floor below.
_RELEVANCE_THRESHOLD = 60
_ACCESSORY_TERMS = (
    "skin", "wrap", "cover", "sleeve", "case", "bag", "pouch",
    "battery", "charger", "adapter", "cable", "dock", "stand",
    "protector", "keyboard cover", "hinge", "replacement", "sticker",
)
# No real laptop lists below this — kills accessories with clean titles.
_MIN_LAPTOP_PRICE_RM = 800.0
# Minimum product_name similarity for a catalog match.
_CATALOG_THRESHOLD = 75
_HISTORY_POINTS = 5

# query → (fetched_at, listings section). Prices move slowly; credits don't.
_CACHE_TTL_S = 6 * 3600
_cache: dict[str, tuple[float, dict]] = {}


def _marketplace_links(query: str) -> dict:
    encoded = quote_plus(query)
    return {
        "shopee_search_url": f"https://shopee.com.my/search?keyword={encoded}",
        "lazada_search_url": f"https://www.lazada.com.my/catalog/?q={encoded}",
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


# ─── Layer 1: own catalog (official price + price history) ───────────────


def _catalog_lookup(query: str, model_code: Optional[str], session: Session) -> dict:
    laptop = None
    if model_code:
        laptop = session.exec(
            select(Laptop).where(Laptop.model_code == model_code)
        ).first()

    best_score = None
    if laptop is None:
        rows = session.exec(select(Laptop)).all()
        # Catalog product_names omit the brand ("ROG Strix G16 …", no
        # "ASUS"), but the agent usually passes brand-qualified names — the
        # unmatched brand token alone drops token_set_ratio below the
        # threshold. Score against both forms and keep the best.
        brands = session.exec(select(LaptopBrand)).all()
        brand_map = {b.id: (b.name or "").lower() for b in brands}
        q = query.lower()
        scored = []
        for l in rows:
            name = (l.product_name or "").lower()
            score = fuzz.token_set_ratio(q, name)
            brand = brand_map.get(l.brand_id)
            if brand:
                score = max(score, fuzz.token_set_ratio(q, f"{brand} {name}"))
            scored.append((score, l))
        scored = [(s, l) for s, l in scored if s >= _CATALOG_THRESHOLD]
        if scored:
            best_score, laptop = max(scored, key=lambda t: t[0])

    if laptop is None:
        return {"found": False}

    history = session.exec(
        select(LaptopPriceHistory)
        .where(LaptopPriceHistory.laptop_id == laptop.id)
        .order_by(LaptopPriceHistory.recorded_at.desc())  # type: ignore[union-attr]
        .limit(_HISTORY_POINTS)
    ).all()

    result = {
        "found": True,
        "product_name": laptop.product_name,
        "model_code": laptop.model_code,
        "official_price_rm": laptop.price_rm,
        "match_score": best_score if best_score is not None else 100,
        "price_history": [
            {"price_rm": h.price_rm, "date": h.recorded_at.date().isoformat()}
            for h in history
        ],
    }
    if best_score is not None:
        # Fuzzy name match, not a model_code hit — the catalog may only have
        # a nearby variant (different chip generation / size / RAM).
        result["note"] = (
            "Closest catalog match by name. Verify product_name is the exact "
            "model the user asked about (chip generation, screen size, RAM) — "
            "if it differs, present it as the closest catalog entry with its "
            "differences, NOT as the requested laptop's official price."
        )
    return result


# ─── Layer 2: live listings via Google Shopping (SerpApi) ────────────────


def _to_listing(item: dict, query: str) -> Optional[dict]:
    title = item.get("title") or ""
    # SerpApi provides the numeric price in extracted_price; the formatted
    # "price" string ("RM4,599.00") is the fallback.
    price = item.get("extracted_price")
    if not isinstance(price, (int, float)):
        price = _parse_price(item.get("price"))
    if not title or price is None or price < _MIN_LAPTOP_PRICE_RM:
        return None
    price = float(price)
    title_lower = title.lower()
    if any(term in title_lower for term in _ACCESSORY_TERMS):
        return None
    relevance = fuzz.token_set_ratio(query.lower(), title_lower)
    if relevance < _RELEVANCE_THRESHOLD:
        return None
    return {
        "title": title,
        "price_rm": price,
        "store": item.get("source"),
        "link": item.get("link") or item.get("product_link"),
        "relevance": round(relevance, 1),
    }


def _live_listings(query: str) -> dict:
    """Returns the live_listings section; only "ok" results are cached."""
    if not settings.serp_api_key:
        return {
            "status": "unavailable",
            "listings": [],
            "note": "Live listings lookup is not configured (SERP_API_KEY missing).",
        }

    cache_key = query.lower()
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]

    try:
        resp = httpx.get(
            _SERPAPI_URL,
            params={
                "engine": "google_shopping",
                "q": query,
                "gl": "my",
                "hl": "en",
                "api_key": settings.serp_api_key,
            },
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("market_price: SerpApi shopping call failed for %r: %s", query, e)
        return {
            "status": "unavailable",
            "listings": [],
            "note": f"Live listings lookup failed ({type(e).__name__}).",
        }

    items = payload.get("shopping_results") or [] if isinstance(payload, dict) else []
    listings = [l for l in (_to_listing(item, query) for item in items) if l]
    listings.sort(key=lambda l: (-l["relevance"], l["price_rm"]))
    # Cap 2 per store so the price range reflects the market, not one shop's
    # catalog dominating Google Shopping's ranking for that query.
    per_store: dict = {}
    diverse = []
    for l in listings:
        if per_store.get(l["store"], 0) < 2:
            per_store[l["store"]] = per_store.get(l["store"], 0) + 1
            diverse.append(l)
        if len(diverse) == _MAX_LISTINGS:
            break
    listings = diverse

    if not listings:
        return {
            "status": "no_match",
            "listings": [],
            "note": "No relevant live listings found on Google Shopping (Malaysia).",
        }

    prices = [l["price_rm"] for l in listings]
    result = {
        "status": "ok",
        "listings": listings,
        "price_min_rm": min(prices),
        "price_max_rm": max(prices),
        "note": (
            "Live Malaysian retail listings via Google Shopping — indicative; "
            "suggest verifying on the store page before buying."
        ),
    }
    _cache[cache_key] = (time.monotonic(), result)
    return result


@tool
def search_malaysian_market_price(product_name: str, model_code: Optional[str] = None) -> dict:
    """
    Look up Malaysian market prices for a laptop, from two sources at once:
    the PickWise catalog (official price + recent price history, scraped from
    the brand's official site) and live retail listings across Malaysian
    stores (Shopee, Lazada, senQ, etc. via Google Shopping).

    Args:
        product_name: Full laptop name (e.g. "ASUS ROG Strix G16").
        model_code: Optional model code for a precise catalog match.

    Returns:
        A dict with:
        - catalog: {found, product_name, model_code, official_price_rm,
          price_history} — the official price on record and up to 5 recent
          price snapshots. found=false when the laptop isn't in the catalog.
        - live_listings: {status, listings, price_min_rm, price_max_rm} —
          status "ok" with up to 5 store listings (title, price_rm, store,
          link, relevance 0-100), or "no_match" / "unavailable".
        - shopee_search_url / lazada_search_url: direct marketplace search
          links — always included, offer them for manual verification.

        When both layers have data, present the official/catalog price and
        the live street-price range together. When only one has data, use it
        and say what's missing. When neither does, say so honestly and share
        the search links. Always cite store names and note prices are
        indicative.
    """
    query = " ".join(part for part in [product_name, model_code] if part).strip()

    try:
        with Session(engine) as session:
            catalog = _catalog_lookup(query, model_code, session)
    except Exception:
        logger.exception("market_price: catalog lookup failed for %r", query)
        catalog = {"found": False, "note": "Catalog lookup failed."}

    return {
        "query": query,
        "catalog": catalog,
        "live_listings": _live_listings(query),
        **_marketplace_links(query),
    }
