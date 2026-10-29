"""
Run a brand's scraper on live pages and print what it returns, as JSON.

Read-only: no session, no database, no LLM. Use it to check a scraper change
against real pages before anything is crawled or processed. Acer is not here:
it reads uploaded HTML out of the database.

Usage:
    python -m app.scripts.scrape_preview asus URL [URL ...]
    python -m app.scripts.scrape_preview asus --crawl [START_URL]   # discovery only
    python -m app.scripts.scrape_preview hp URL > out.json          # summary on stderr

Exits 1 if any URL (or any variant) failed.
"""
import argparse
import asyncio
import contextlib
import json
import sys

from app.scraper.apple_scraper import crawl_apple_specs_links, scrape_official_website
from app.scraper.asus_scraper import crawl_asus_specs_links, scrape_asus_laptop_specs
from app.scraper.hp_scraper import crawl_hp_specs_links, scrape_hp_laptop_specs


async def _apple(url: str) -> list[dict]:
    # Apple returns one dict per page; everything else returns a list
    return [await scrape_official_website(url, "apple", None)]


SCRAPE = {
    "apple": _apple,
    "asus": lambda url: scrape_asus_laptop_specs(url, None),
    "hp": lambda url: scrape_hp_laptop_specs(url, None),
}
CRAWL = {
    "apple": crawl_apple_specs_links,
    "asus": crawl_asus_specs_links,
    "hp": crawl_hp_specs_links,
}


async def _main(args, stdout) -> int:
    if args.crawl:
        urls = await CRAWL[args.brand](*args.urls[:1])
        print(json.dumps(urls, indent=2), file=stdout)
        print(f"{len(urls)} URL(s)", file=sys.stderr)
        return 0 if urls else 1

    out, failed = {}, 0
    for n, url in enumerate(args.urls):
        if n:
            await asyncio.sleep(1.5)  # same pause as a bulk run between live fetches
        try:
            variants = await SCRAPE[args.brand](url)
        except Exception as exc:  # report it and move on to the next URL
            variants = [{"status": "failed", "error": f"{type(exc).__name__}: {exc}"}]
        out[url] = variants
        bad = [v for v in variants if v.get("status") == "failed"]
        failed += bool(bad) or not variants
        note = f"FAILED: {bad[0].get('error')}" if bad else f"{len(variants)} variant(s)"
        print(f"{url}  {note}", file=sys.stderr)

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str), file=stdout)
    print(f"{len(args.urls) - failed}/{len(args.urls)} URL(s) succeeded", file=sys.stderr)
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("brand", choices=sorted(SCRAPE))
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--crawl", action="store_true", help="run discovery instead (optional START_URL)")
    args = parser.parse_args()
    # A Windows console's code page (GBK here) can't encode the scrapers'
    # emoji log lines or the pages' ®/nbsp, and a print error inside a scraper
    # surfaces as a failed scrape
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if not args.crawl and not args.urls:
        parser.error("give at least one URL, or --crawl")
    # the scrapers print their own log lines; keep stdout pure JSON
    real_stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        code = asyncio.run(_main(args, real_stdout))
    sys.exit(code)


if __name__ == "__main__":
    main()
