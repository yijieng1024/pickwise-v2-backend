"""
What a re-scrape of the ROG and HP pages would do to the catalog (plan step 11, decisions
6, 7, 15 and 16). A dry run: it fetches every stored rog.asus.com and www.hp.com target
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
  attach      no laptop holds the SKU, and a laptop of the same brand, product line and
              specs exists: the SKU is attached to it and that laptop is updated
  insert      no laptop holds the SKU and none is the same machine: a new laptop, held by
              the go-live check. Listed with the closest laptop by name
  slug        the page's specs cannot be read, so the processor matches by the LLM's slug.
              This script calls no LLM and cannot say which laptop that is

"SKU", "attach" and "insert" are decided by the functions the processor itself calls
(app/laptops/sku_match.py, and the same SKU rule as engine._laptop_by_sku), from the page's
own spec table, so they are what a real run does. One thing is assumed: that the LLM
extracts one variant from each page. A page it splits into several is matched by slug.
"""
import argparse
import asyncio
import collections
import re
import sys
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from dotenv import load_dotenv
from rapidfuzz import fuzz
from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopSku
from app.laptops.price_guide import sku_key
from app.laptops.sku_match import page_specs, same_machine
from app.processor.engine import raw_sku
from app.scraper.asus_scraper import scrape_asus_laptop_specs
from app.scraper.bulk_scraper import LIVE_FETCH_PAUSE_S, RESCRAPED_HOSTS, rescraped_every_run, unchanged
from app.scraper.hp_scraper import scrape_hp_laptop_specs
from app.scraper.models import RawScrapLaptop, ScrapeTarget

_LAPTOPS = Laptop.__table__
_BRANDS = LaptopBrand.__table__
_TARGETS = ScrapeTarget.__table__
_RAW = RawScrapLaptop.__table__
_SKUS = LaptopSku.__table__

_SCRAPE = {"rog.asus.com": scrape_asus_laptop_specs, "www.hp.com": scrape_hp_laptop_specs}
_LAPTOP_FIELDS = ("id", "brand_id", "product_name", "model_code", "manufacturer_sku", "status", "price_rm",
                  "processor_model", "gpu_model", "ram_gb", "ssd_gb", "display_size_inch")


def _by_sku(laptops: list[Any], sku: str) -> Optional[Any]:
    """engine._laptop_by_sku, over rows in memory: -W and -WP are one SKU, the exact form
    wins, and a laptop's own SKU is asked before the others it carries."""
    key = sku_key(sku)
    forms = {key, f"{key}P"} if key.endswith("W") else {key}
    for held in (lambda l: [l.manufacturer_sku] if l.manufacturer_sku else [], lambda l: l.skus):
        holders = [(s, l) for l in laptops for s in held(l) if s.upper() in forms]
        if holders:
            return next((l for s, l in holders if s.upper() == sku.upper()), holders[0][1])
    return None


def _closest(laptops: list[Any], name: str) -> tuple[Optional[Any], int]:
    """The laptop closest to a page name, for a reader to tell a new model from a duplicate."""
    scored = [(fuzz.token_set_ratio(name, l.product_name), fuzz.ratio(name, l.product_name), str(l.id), l)
              for l in laptops]
    if not scored:
        return None, 0
    score, _, _, laptop = max(scored, key=lambda item: item[:3])
    return laptop, round(score)


def _price(features: Any) -> Optional[float]:
    """The price a page shows ("RM4,999.00"), or None."""
    shown = features.get("Price") if isinstance(features, dict) else None
    digits = re.sub(r"[^\d.]", "", shown) if isinstance(shown, str) else ""
    return float(digits) if digits and float(digits) > 0 else None


def plan(variants: Iterable[Any], laptops: Iterable[Any]) -> list[dict]:
    """
    One row per fetched variant, in the order given, which is the order a run processes
    them. Decides everything; reads and writes nothing.

    variants: source_url, brand_id, name, sku, features (the page's spec table), stored
    ("unchanged", "changed" or "new": against the raw row of that source_url).
    laptops: the _LAPTOP_FIELDS, and `skus`, the other SKUs the laptop carries.

    A laptop inserted for one page is there for the next one, as in a real run: a second
    colour page of a new machine attaches to the first one's laptop.
    """
    laptops = [SimpleNamespace(**{k: getattr(l, k) for k in _LAPTOP_FIELDS}, skus=list(getattr(l, "skus", [])))
               for l in laptops]
    rows = []
    for v in variants:
        row = {"source_url": v.source_url, "name": v.name, "sku": v.sku, "outcome": None, "laptop": None,
               "score": None, "price": _price(v.features)}
        rows.append(row)
        if v.stored == "unchanged":
            row["outcome"] = "unchanged"
            continue
        if not v.sku:
            row["outcome"] = "no SKU" if v.stored != "new" else "slug"
            continue
        holder = _by_sku(laptops, v.sku)
        if holder is not None:
            row.update(outcome="SKU", laptop=holder)
            continue
        specs = page_specs(v.features)
        if specs is None:
            row["outcome"] = "slug"
            continue
        brand = [l for l in laptops if l.brand_id == v.brand_id]
        same = same_machine(v.name, specs, brand)
        if same is not None:
            row.update(outcome="attach", laptop=same)
            same.skus.append(v.sku)
            continue
        closest, score = _closest(brand, v.name)
        row.update(outcome="insert", laptop=closest, score=score)
        laptops.append(SimpleNamespace(
            id=f"new laptop of {v.sku}", brand_id=v.brand_id, product_name=v.name, model_code="(new)",
            manufacturer_sku=v.sku, status="inactive", price_rm=row["price"] or 0.0, processor_model=specs.cpu,
            gpu_model=specs.gpu, ram_gb=specs.ram_gb, ssd_gb=specs.storage_gb, display_size_inch=specs.screen_inch,
            skus=[],
        ))
    return rows


def price_differences(rows: list[dict]) -> list[dict]:
    """Laptops whose pages in this run show different prices (decision 16): the laptop
    keeps the lowest."""
    by_laptop: dict = collections.defaultdict(list)
    for row in rows:
        if row["outcome"] in ("SKU", "attach") and row["price"]:
            by_laptop[str(row["laptop"].id)].append(row)
    return [
        {"laptop": pages[0]["laptop"], "kept": min(p["price"] for p in pages),
         "prices": {p["sku"]: p["price"] for p in pages}}
        for pages in by_laptop.values() if len({p["price"] for p in pages}) > 1
    ]


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
    """Every ASUS and HP laptop, in any status, with the SKUs it carries: the processor's
    lookups read them all. A database from before the laptop_skus table has none."""
    carried: dict = collections.defaultdict(list)
    if connection.execute(text("select to_regclass('public.laptop_skus')")).scalar() is not None:
        for row in connection.execute(select(_SKUS.c.laptop_id, _SKUS.c.sku)):
            carried[row.laptop_id].append(row.sku)
    return [
        SimpleNamespace(**dict(l._mapping), skus=carried[l.id])
        for l in connection.execute(
            select(*[_LAPTOPS.c[name] for name in _LAPTOP_FIELDS])
            .join(_BRANDS, _BRANDS.c.id == _LAPTOPS.c.brand_id)
            .where(func.lower(_BRANDS.c.name).in_(["asus", "hp"]))
            .order_by(_LAPTOPS.c.product_name, _LAPTOPS.c.id)
        )
    ]


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
                sku=raw_sku(fresh.raw_specs_dump), features=fresh.raw_specs_dump["scraped_features"],
                stored="new" if old is None else "unchanged" if unchanged(old, fresh) else "changed",
            ))
    return variants, failures


def _laptop_text(laptop: Optional[Any]) -> str:
    if laptop is None:
        return "-"
    return f"{laptop.id}  {laptop.status}  {laptop.model_code}  RM{laptop.price_rm:,.0f}  {laptop.product_name}"


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

    labels = {"SKU": "updates", "attach": "attaches to", "insert": "closest by name"}
    for row in rows:
        price = f"RM{row['price']:,.0f}" if row["price"] else "no price"
        print(f"{row['outcome'].upper():9}  {row['sku'] or '-':18}  {price:9}  {row['name']}")
        print(f"{'':9}  {row['source_url']}")
        if row["outcome"] in labels:
            score = f"  (name {row['score']})" if row["score"] is not None else ""
            print(f"{'':9}  {labels[row['outcome']]}: {_laptop_text(row['laptop'])}{score}")
    for page, error in failures:
        print(f"FAILED     {page}  {error}")
    for difference in price_differences(rows):
        prices = ", ".join(f"{sku} RM{price:,.0f}" for sku, price in difference["prices"].items())
        print(f"PRICES     keeps RM{difference['kept']:,.0f}  {_laptop_text(difference['laptop'])}  |  {prices}")

    for host in sorted(RESCRAPED_HOSTS):
        counts = collections.Counter(r["outcome"] for r in rows if r["source_url"].split("/")[2].lower() == host)
        pages = sum(1 for t in targets if t.url.split("/")[2].lower() == host)
        failed = sum(1 for page, _ in failures if page.split("/")[2].lower() == host)
        print(f"{host}: {pages} pages, {sum(counts.values())} variants, {failed} failed fetches | "
              + ", ".join(f"{name} {counts[name]}" for name in ("unchanged", "no SKU", "SKU", "attach", "insert", "slug")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
