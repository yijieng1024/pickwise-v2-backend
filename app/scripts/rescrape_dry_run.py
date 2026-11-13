"""
What the first re-scrape of the ROG and HP pages would do to the catalog (plan step 11,
decisions 6 and 7). A dry run: it fetches every stored rog.asus.com and www.hp.com target
and reads the database, and writes nothing. There is no --apply.

    python -m app.scripts.rescrape_dry_run                 # every target, about 10 minutes
    python -m app.scripts.rescrape_dry_run --limit 5       # the first 5 targets of each site

Reads TEST_DATABASE_URL unless RESCRAPE_DRY_RUN_TARGET=production is set, and checks that
the URL is the database it claims to be (app/common/db_target.py). The connection is
opened read-only.

For each variant on each page, one of:
  unchanged   as stored (name, prices, specs): the scrape keeps the row, nothing is processed
  no SKU      the page fetched now carries no SKU: the stored row is kept, nothing is processed
  SKU         a laptop holds the page's SKU: the processor updates that laptop
  slug        no laptop holds the SKU, and the closest laptop by name has none either: the
              processor would update it if the LLM words the slug as before, and store the SKU
  insert      a new laptop: either no laptop is close by name, or the closest one already
              holds another SKU (the processor never puts two SKUs on one laptop)

The slug is the LLM's wording, and this script calls no LLM, so "slug" and "insert" are
predicted from the names: the closest existing laptop of the brand by RapidFuzz
token_set_ratio, a match at NAME_MATCH or more. Read the names in the table before trusting
a row. A raw row the LLM splits into several variants is matched by slug alone, whatever
its SKU; that cannot be predicted here either.
"""
import argparse
import asyncio
import collections
import sys
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from dotenv import load_dotenv
from rapidfuzz import fuzz
from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop
from app.laptops.price_guide import sku_key
from app.processor.engine import raw_sku
from app.scraper.asus_scraper import scrape_asus_laptop_specs
from app.scraper.bulk_scraper import LIVE_FETCH_PAUSE_S, RESCRAPED_HOSTS, rescraped_every_run, unchanged
from app.scraper.hp_scraper import scrape_hp_laptop_specs
from app.scraper.models import RawScrapLaptop, ScrapeTarget

_LAPTOPS = Laptop.__table__
_BRANDS = LaptopBrand.__table__
_TARGETS = ScrapeTarget.__table__
_RAW = RawScrapLaptop.__table__

# A page name and a laptop name this close are read as one machine.
NAME_MATCH = 85

_SCRAPE = {"rog.asus.com": scrape_asus_laptop_specs, "www.hp.com": scrape_hp_laptop_specs}


def _by_sku(laptops: list[Any], sku: str) -> Optional[Any]:
    """engine._laptop_by_sku, over rows in memory: -W and -WP are one SKU, the exact form wins."""
    key = sku_key(sku)
    forms = {key, f"{key}P"} if key.endswith("W") else {key}
    holders = [l for l in laptops if l.manufacturer_sku and l.manufacturer_sku.upper() in forms]
    return next((l for l in holders if l.manufacturer_sku.upper() == sku.upper()), holders[0] if holders else None)


def _closest(laptops: list[Any], name: str, sku: Optional[str]) -> tuple[Optional[Any], float, float]:
    """The laptop closest to a page name, how close (0 to 100), and the tie-breaker. The
    page's own SKU is left out of the name first: it is compared on its own, and a long SKU
    that differs by one letter would otherwise decide the score. Laptops that tie are told
    apart by the whole name."""
    bare = " ".join(name.replace(f"({sku})", " ").replace(sku, " ").split()) if sku else name
    scored = [(fuzz.token_set_ratio(bare, l.product_name), fuzz.ratio(name, l.product_name), str(l.id), l)
              for l in laptops]
    if not scored:
        return None, 0, 0
    score, whole, _, laptop = max(scored, key=lambda item: item[:3])
    return laptop, score, whole


def plan(variants: Iterable[Any], laptops: Iterable[Any]) -> list[dict]:
    """
    One row per fetched variant, in the order given. Decides everything; reads and writes
    nothing.

    variants: source_url, brand_id, name, sku, stored ("unchanged", "changed" or "new":
    against the raw row of that source_url). laptops: id, brand_id, product_name,
    model_code, manufacturer_sku, status.

    A laptop matched by slug gets the SKU, as the processor stores it, so another variant
    of the same run that is closest to the same laptop is an insert. The closest names are
    matched first, whatever the page order: a page named exactly as a laptop takes it
    before a page that is only similar.
    """
    laptops = [SimpleNamespace(**{k: getattr(l, k) for k in
                                  ("id", "brand_id", "product_name", "model_code", "manufacturer_sku", "status")})
               for l in laptops]
    rows, by_name = [], []
    for v in variants:
        row = {"source_url": v.source_url, "name": v.name, "sku": v.sku, "outcome": None, "laptop": None,
               "score": None, "note": ""}
        rows.append(row)
        if v.stored == "unchanged":
            row["outcome"] = "unchanged"
            continue
        if not v.sku and v.stored != "new":
            row["outcome"] = "no SKU"
            continue
        holder = _by_sku(laptops, v.sku) if v.sku else None
        if holder is not None:
            row.update(outcome="SKU", laptop=holder)
            continue
        closest, score, whole = _closest([l for l in laptops if l.brand_id == v.brand_id], v.name, v.sku)
        row.update(laptop=closest, score=round(score))
        by_name.append((-score, -whole, len(by_name), v, row))

    for _, _, _, v, row in sorted(by_name, key=lambda item: item[:3]):
        closest = row["laptop"]
        if closest is None or row["score"] < NAME_MATCH:
            row.update(outcome="insert", note="no laptop this close by name")
        elif closest.manufacturer_sku and v.sku:
            row.update(outcome="insert", note=f"closest laptop holds {closest.manufacturer_sku}")
        else:
            row["outcome"] = "slug"
            closest.manufacturer_sku = closest.manufacturer_sku or v.sku
    return rows


def read_targets(connection: Connection) -> list[Any]:
    """Every active target on a re-scraped site, with its brand."""
    return [t for t in connection.execute(
        select(_TARGETS.c.url, _TARGETS.c.brand_id).where(_TARGETS.c.is_active.is_(True)).order_by(_TARGETS.c.url)
    ) if rescraped_every_run(t.url)]


def read_stored(connection: Connection) -> dict[str, Any]:
    """The raw rows of the re-scraped sites, by source_url."""
    return {r.source_url: r for r in connection.execute(
        select(_RAW.c.source_url, _RAW.c.raw_product_name, _RAW.c.raw_prices, _RAW.c.raw_specs_dump)
        .where(or_(*[_RAW.c.source_url.like(f"https://{host}/%") for host in sorted(RESCRAPED_HOSTS)]))
    )}


def read_laptops(connection: Connection) -> list[Any]:
    """Every ASUS and HP laptop, in any status: the processor's lookups read them all."""
    return list(connection.execute(
        select(_LAPTOPS.c.id, _LAPTOPS.c.brand_id, _LAPTOPS.c.product_name, _LAPTOPS.c.model_code,
               _LAPTOPS.c.manufacturer_sku, _LAPTOPS.c.status)
        .join(_BRANDS, _BRANDS.c.id == _LAPTOPS.c.brand_id)
        .where(func.lower(_BRANDS.c.name).in_(["asus", "hp"]))
        .order_by(_LAPTOPS.c.product_name, _LAPTOPS.c.id)
    ))


async def fetch(targets: list[Any], stored: dict[str, Any]) -> tuple[list[Any], list[tuple[str, str]]]:
    """Each target's variants as plan reads them, and the pages that failed."""
    variants, failures = [], []
    for n, target in enumerate(targets, start=1):
        host = target.url.split("/")[2].lower()
        print(f"fetching {n}/{len(targets)} {target.url}", file=sys.stderr)
        results = await _SCRAPE[host](target.url, None)
        await asyncio.sleep(LIVE_FETCH_PAUSE_S)
        for result in results:
            if result.get("status") == "failed":
                failures.append((target.url, result.get("error", "unknown error")))
                continue
            source_url = target.url + result.get("source_url_suffix", "")
            fresh = SimpleNamespace(raw_product_name=result.get("product_name", "Unknown Model"),
                                    raw_prices=result.get("raw_prices_list", []),
                                    raw_specs_dump={"scraped_features": result.get("raw_specs", [])})
            old = stored.get(source_url)
            variants.append(SimpleNamespace(
                source_url=source_url, brand_id=target.brand_id, name=fresh.raw_product_name,
                sku=raw_sku(fresh.raw_specs_dump),
                stored="new" if old is None else "unchanged" if unchanged(old, fresh) else "changed",
            ))
    return variants, failures


def _laptop_text(laptop: Optional[Any]) -> str:
    return f"{laptop.id}  {laptop.status}  {laptop.product_name}" if laptop is not None else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only the first N targets of each site")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("RESCRAPE_DRY_RUN_TARGET")
    print(f"dry run, reading {host_of(url)}; nothing is written")

    engine = create_engine(url)
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        targets, stored, laptops = read_targets(connection), read_stored(connection), read_laptops(connection)
    engine.dispose()

    if args.limit:
        seen: collections.Counter = collections.Counter()
        kept = []
        for target in targets:
            host = target.url.split("/")[2].lower()
            seen[host] += 1
            if seen[host] <= args.limit:
                kept.append(target)
        targets = kept

    variants, failures = asyncio.run(fetch(targets, stored))
    rows = plan(variants, laptops)

    for row in rows:
        score = f"  name {row['score']}" if row["score"] is not None else ""
        print(f"{row['outcome'].upper():9}  {row['sku'] or '-':18}  {row['name']}")
        print(f"{'':9}  {row['source_url']}")
        if row["outcome"] in ("SKU", "slug", "insert"):
            label = "closest" if row["outcome"] == "insert" else "updates"
            print(f"{'':9}  {label}: {_laptop_text(row['laptop'])}{score}  {row['note']}".rstrip())
    for page, error in failures:
        print(f"FAILED     {page}  {error}")

    for host in sorted(RESCRAPED_HOSTS):
        counts = collections.Counter(r["outcome"] for r in rows if r["source_url"].split("/")[2].lower() == host)
        pages = sum(1 for t in targets if t.url.split("/")[2].lower() == host)
        failed = sum(1 for page, _ in failures if page.split("/")[2].lower() == host)
        print(f"{host}: {pages} pages, {sum(counts.values())} variants, {failed} failed fetches | "
              + ", ".join(f"{name} {counts[name]}" for name in ("unchanged", "no SKU", "SKU", "slug", "insert")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
