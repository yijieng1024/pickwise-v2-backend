"""
Fill `laptops.manufacturer_sku` from the SKU in a ROG or TUF laptop's name. One-off:
the processor only fills it on a raw row that carries a SKU, and every stored laptop
predates that column.

    python -m app.scripts.backfill_manufacturer_sku            # prints, writes nothing
    python -m app.scripts.backfill_manufacturer_sku --apply

Reads and writes TEST_DATABASE_URL unless SKU_BACKFILL_TARGET=production is set, and
checks that the URL is the database it claims to be (app/common/db_target.py). Safe to
run again: a laptop that has a SKU is skipped. Run it after the duplicates are
suspended: until then a SKU named by two laptops is skipped, and so is a suspended
laptop, because a scrape of that SKU should update the row people can see.

A Core UPDATE, so the mapper event doesn't stamp `updated_at`: a SKU changes no score,
and the Dashboard would call every Pick Score stale.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import collections
import re
import sys
from typing import Any, Iterable

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.laptops.price_guide import sku_key

_LAPTOPS = Laptop.__table__
_BRANDS = LaptopBrand.__table__
_LINE = re.compile(r"\b(?:ROG|TUF)\b")
_SKU = re.compile(r"\b[A-Z]{1,3}\d{3,4}[A-Z]{0,3}-[A-Z0-9]{4,}\b")
# Both GU605C rows say -W, but one raw page is -W and the other -WP, and nothing says
# which row came from which.
_HELD_BACK = {"GU605C-PQR045W"}


def read_laptops(connection: Connection) -> list[Any]:
    """Every ASUS laptop: a SKU stored on any of them is taken."""
    return list(connection.execute(
        select(_LAPTOPS.c.id, _LAPTOPS.c.product_name, _LAPTOPS.c.manufacturer_sku, _LAPTOPS.c.status)
        .join(_BRANDS, _BRANDS.c.id == _LAPTOPS.c.brand_id)
        .where(func.lower(_BRANDS.c.name) == "asus")
        .order_by(_LAPTOPS.c.product_name, _LAPTOPS.c.id)
    ))


def plan(laptops: Iterable[Any]) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]]]:
    """(laptop, SKU to write) and (laptop, why not). Decides everything; writes nothing."""
    laptops = list(laptops)
    stored = {sku_key(l.manufacturer_sku) for l in laptops if l.manufacturer_sku}
    # -W and -WP are one SKU to the guides and the processor, so one laptop may hold it.
    named = collections.Counter(
        sku_key(sku) for l in laptops if l.status != LaptopStatus.SUSPENDED.value
        for sku in _SKU.findall(l.product_name)
    )
    writes, skips = [], []
    for laptop in laptops:
        if not _LINE.search(laptop.product_name):
            continue  # not a skip: this script is not about these laptops
        skus = _SKU.findall(laptop.product_name)
        key = sku_key(skus[0]) if skus else None
        if laptop.manufacturer_sku:
            skips.append((laptop, f"already has {laptop.manufacturer_sku}"))
        elif laptop.status == LaptopStatus.SUSPENDED.value:
            skips.append((laptop, "suspended"))
        elif not skus:
            skips.append((laptop, "no SKU in the name"))
        elif len(skus) > 1:
            skips.append((laptop, f"more than one SKU in the name: {', '.join(skus)}"))
        elif key in _HELD_BACK:
            skips.append((laptop, f"{skus[0]}: held back until -W/-WP is known"))
        elif key in stored:
            skips.append((laptop, f"{skus[0]}: another laptop holds it"))
        elif named[key] > 1:
            skips.append((laptop, f"{skus[0]}: {named[key]} laptops name it"))
        else:
            writes.append((laptop, skus[0]))
    return writes, skips


def apply(connection: Connection, writes: list[tuple[Any, str]]) -> int:
    """Rows changed. The caller owns the transaction, so a failure writes nothing."""
    return sum(
        connection.execute(
            update(_LAPTOPS)
            .where(_LAPTOPS.c.id == laptop.id, _LAPTOPS.c.manufacturer_sku.is_(None))
            .values(manufacturer_sku=sku)
        ).rowcount
        for laptop, sku in writes
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("SKU_BACKFILL_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")

    engine = create_engine(url)
    with engine.begin() as connection:
        writes, skips = plan(read_laptops(connection))
        for laptop, sku in writes:
            print(f"WRITE {laptop.id}  {sku}  {laptop.product_name}")
        for laptop, reason in skips:
            print(f"SKIP  {laptop.id}  {reason}  {laptop.product_name}")
        changed = apply(connection, writes) if args.apply else 0
    engine.dispose()

    print(f"{len(writes)} to write, {len(skips)} skipped, {changed} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
