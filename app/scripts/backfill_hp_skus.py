"""
Give an active HP laptop with no SKU the SKU its stored HP page shows (`Product Info / Sku`
in the raw row). One-off: these laptops were processed before the SKU table existed, so the
weekly "SKUs no longer on their page" check cannot see them. Plain comparison, no LLM.

    python -m app.scripts.backfill_hp_skus            # prints, writes nothing
    python -m app.scripts.backfill_hp_skus --apply

Attached only on an exact match: the page's name equals the laptop's (case, commas and
spacing aside) AND the five specs the processor's SKU matching reads are equal
(app/laptops/sku_match.py). A SKU another laptop already holds is skipped, and so is a
laptop with two exact pages or a page that fits two laptops. Same product line and specs
under another name is printed as LOOSE for an admin to decide, never attached.

Writes `laptops.manufacturer_sku` and one `laptop_skus` row, as the processor does for a
page it reads. Core statements, so the mapper event doesn't stamp `updated_at`: a SKU
changes no score. Reads and writes TEST_DATABASE_URL unless HP_SKU_BACKFILL_TARGET=production
is set, and checks that the URL is the database it claims to be (app/common/db_target.py).
Safe to run again: a laptop with a SKU is skipped.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import collections
import sys
import uuid
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopSku, LaptopStatus
from app.laptops.price_guide import sku_key
from app.laptops.sku_match import laptop_specs, page_colour, page_specs, product_line
from app.processor.engine import raw_sku
from app.scraper.models import RawScrapLaptop

_LAPTOPS, _BRANDS, _SKUS, _RAW = Laptop.__table__, LaptopBrand.__table__, LaptopSku.__table__, RawScrapLaptop.__table__
_SPEC_FIELDS = ("processor_model", "gpu_model", "ram_gb", "ssd_gb", "display_size_inch")


def _name(text: str) -> str:
    return " ".join((text or "").replace(",", " ").lower().split())


def read(connection: Connection) -> tuple[list[Any], list[Any], set[str]]:
    """(HP laptops, in any status), (HP raw rows), (every SKU held, sku_key form)."""
    laptops = [SimpleNamespace(**dict(r._mapping)) for r in connection.execute(
        select(_LAPTOPS.c.id, _LAPTOPS.c.product_name, _LAPTOPS.c.status, _LAPTOPS.c.manufacturer_sku,
               *[_LAPTOPS.c[f] for f in _SPEC_FIELDS])
        .join(_BRANDS, _BRANDS.c.id == _LAPTOPS.c.brand_id).where(func.lower(_BRANDS.c.name) == "hp")
        .order_by(_LAPTOPS.c.product_name, _LAPTOPS.c.id))]
    with_rows = {r.laptop_id for r in connection.execute(select(_SKUS.c.laptop_id))}
    for l in laptops:
        l.has_sku = bool(l.manufacturer_sku) or l.id in with_rows
    raws = list(connection.execute(select(_RAW.c.source_url, _RAW.c.raw_product_name, _RAW.c.raw_specs_dump)
                                   .where(_RAW.c.source_url.like("https://www.hp.com/%")).order_by(_RAW.c.source_url)))
    held = {sku_key(r.sku) for r in connection.execute(select(_SKUS.c.sku))}
    held |= {sku_key(l.manufacturer_sku) for l in laptops if l.manufacturer_sku}
    return laptops, raws, held


def plan(laptops: list[Any], raws: list[Any], held: set[str]) -> tuple[list, list, list]:
    """(laptop, SKU, colour) to write, (laptop, SKU, what differs) loose, (laptop, why) skipped.
    Decides everything; writes nothing."""
    pages = []
    for r in raws:
        features = (r.raw_specs_dump or {}).get("scraped_features")
        sku = raw_sku(r.raw_specs_dump)
        if sku and sku_key(sku) not in held:
            pages.append((r, sku, page_specs(features), page_colour(features)))
    todo = [l for l in laptops if l.status == LaptopStatus.ACTIVE.value and not l.has_sku]
    exact, loose = collections.defaultdict(list), collections.defaultdict(list)
    for l in todo:
        specs = laptop_specs(l)
        for r, sku, page, colour in pages:
            same_name, same_specs = _name(r.raw_product_name) == _name(l.product_name), page is not None and page == specs
            if same_name and same_specs:
                exact[l.id].append((sku, colour))
            elif same_specs and product_line(r.raw_product_name) == product_line(l.product_name):
                loose[l.id].append((sku, f"name: page {r.raw_product_name!r}"))
            elif same_name:
                loose[l.id].append((sku, f"specs: page {page}, laptop {specs}"))
    fits = collections.Counter(sku_key(sku) for found in exact.values() for sku, _ in set(found))
    writes, loose_out, skips = [], [], []
    for l in todo:
        found = sorted(set(exact.get(l.id, [])))
        if len({sku_key(s) for s, _ in found}) > 1:
            skips.append((l, "two exact pages: " + ", ".join(s for s, _ in found)))
        elif found and fits[sku_key(found[0][0])] > 1:
            skips.append((l, f"{found[0][0]} fits {fits[sku_key(found[0][0])]} laptops"))
        elif found:
            writes.append((l, *found[0]))
        elif loose.get(l.id):
            loose_out += [(l, sku, why) for sku, why in loose[l.id]]
        else:
            skips.append((l, "no stored page with this name or these specs"))
    return writes, loose_out, skips


def apply(connection: Connection, writes: list) -> int:
    """Laptops given a SKU. The caller owns the transaction, so a failure writes nothing."""
    done = 0
    for laptop, sku, colour in writes:
        changed = connection.execute(update(_LAPTOPS).where(_LAPTOPS.c.id == laptop.id, _LAPTOPS.c.manufacturer_sku.is_(None))
                                     .values(manufacturer_sku=sku)).rowcount
        if changed:
            connection.execute(insert(_SKUS).values(id=uuid.uuid4(), laptop_id=laptop.id, sku=sku, colour=colour))
            done += 1
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("HP_SKU_BACKFILL_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")
    engine = create_engine(url)
    with engine.begin() as connection:
        writes, loose, skips = plan(*read(connection))
        for laptop, sku, _ in writes:
            print(f"WRITE {sku:<10} {laptop.product_name}")
        for laptop, sku, why in loose:
            print(f"LOOSE {sku:<10} {laptop.product_name} | differs in {why}")
        for laptop, why in skips:
            print(f"SKIP  {laptop.product_name} | {why}")
        changed = apply(connection, writes) if args.apply else 0
    engine.dispose()
    print(f"{len(writes)} to write, {len({l.id for l, _, _ in loose})} loose, {len(skips)} skipped, {changed} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
