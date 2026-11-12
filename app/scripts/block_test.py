"""
Block test (plan step 11): can this machine's IP read the manufacturer sites?

Fetches one product page per site through the scrapers' own fetch and parse
code, and prints the status code, the response size and whether the laptop
data was in the page. A 200 that is a challenge or captcha page has no laptop
data, so it counts as blocked. No database, no secrets, no writes.

    python -m app.scripts.block_test

Exits 1 if any site was blocked.
"""
import asyncio
import sys

from scrapling.fetchers import Fetcher

from app.scraper.asus_scraper import scrape_asus_laptop_specs
from app.scraper.hp_scraper import scrape_hp_laptop_specs

PAGES = {
    "www.asus.com": (scrape_asus_laptop_specs, "https://www.asus.com/my/laptops/for-home/vivobook/asus-vivobook-15-a1504/"),
    "rog.asus.com": (scrape_asus_laptop_specs, "https://rog.asus.com/my/laptops/rog-strix/rog-strix-g16-2026/"),
    "www.hp.com": (scrape_hp_laptop_specs, "https://www.hp.com/my-en/shop/hp-laptop-15-fc1103au-d9yc9pa.html"),
}

# Every response the scrapers' fetch gets, so the status and size of a refused
# page are printed too (fetch_html raises on anything but 200).
_responses: list[tuple[int, int, str]] = []
_get = Fetcher.get


def _recording_get(url, **kwargs):
    page = _get(url, **kwargs)
    _responses.append((page.status, len(page.body), url))
    return page


async def _check(site: str, scrape, url: str) -> bool:
    _responses.clear()
    try:
        variants = await scrape(url, None)
    except Exception as exc:
        variants = [{"status": "failed", "error": f"{type(exc).__name__}: {exc}"}]
    for status, size, fetched in _responses:
        print(f"{site}: HTTP {status}, {size:,} bytes, {fetched}")
    if not _responses:
        print(f"{site}: no response at all")
    good = [v for v in variants if v.get("status") == "success" and v.get("raw_specs")]
    if good:
        v = good[0]
        print(f"{site}: OK, product data present: {v['product_name']!r}, "
              f"{len(good)} variant(s), {len(v['raw_specs'])} spec fields, price {v['raw_prices_list'][0]['price']}")
    else:
        error = next((v.get("error") for v in variants if v.get("error")), "no variants returned")
        print(f"{site}: BLOCKED or changed, no product data: {error}")
    return bool(good)


async def _main() -> int:
    Fetcher.get = _recording_get
    results = {}
    for site, (scrape, url) in PAGES.items():
        results[site] = await _check(site, scrape, url)
    print("SUMMARY: " + ", ".join(f"{site} {'ok' if ok else 'BLOCKED'}" for site, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
