"""
Give every MacBook Pro row the line token in `model_code` and the name built by
app/processor/apple_name.py. One-off: those rows predate the token, so the next Apple
run would not match them and would insert a second copy of each.

    python -m app.scripts.backfill_apple_line            # prints, writes nothing
    python -m app.scripts.backfill_apple_line --apply

Reads and writes TEST_DATABASE_URL unless APPLE_BACKFILL_TARGET=production is set
(app/common/db_target.py). Safe to run again: a row already in its new form is skipped.
Air and Neo are not touched (one line each, no token).

Which line a row is: there is no link from a laptop to its raw page, so
- a row created before the split pages were first scraped came from the one page that
  existed, /macbook-pro/specs/, the Liquid Retina XDR machine. That includes c07480ce,
  which today's PROLED page overwrote: Jack decided it is restored to the June machine.
- a row created after says its line in `display_type` (PROLED or Liquid Retina XDR);
  one that says neither is skipped, never guessed.

Only model_code and product_name. A Core UPDATE, so the mapper event doesn't stamp
`updated_at` and the Dashboard does not call every Pick Score stale.
"""
import argparse
import sys
from datetime import datetime
from typing import Any, Iterable, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop
from app.processor.apple_name import build_name, product_and_line, with_line_token

_LAPTOPS = Laptop.__table__
_BRANDS = LaptopBrand.__table__
# The first PROLED / Liquid Retina page (raw 282c613b) was scraped 2026-11-03 06:52:08 UTC.
SPLIT_AT = datetime(2026, 11, 3, 6, 52)
_OLD_PAGE = "https://www.apple.com/my/macbook-pro/specs/"


def read_laptops(connection: Connection) -> list[Any]:
    """Every Apple laptop: a code on any of them is taken."""
    return list(connection.execute(
        select(*(_LAPTOPS.c[n] for n in (
            "id", "model_code", "product_name", "status", "created_at", "display_type",
            "display_size_inch", "processor_model", "cpu_cores", "gpu_cores", "ram_gb", "ssd_gb",
        )))
        .join(_BRANDS, _BRANDS.c.id == _LAPTOPS.c.brand_id)
        .where(func.lower(_BRANDS.c.name) == "apple")
        .order_by(_LAPTOPS.c.created_at, _LAPTOPS.c.model_code)
    ))


def line_of(laptop: Any) -> Optional[str]:
    if laptop.created_at.replace(tzinfo=None) < SPLIT_AT:
        return product_and_line(_OLD_PAGE)[1]
    shown = (laptop.display_type or "").lower()
    if "proled" in shown:
        return "proled"
    if "liquid retina xdr" in shown:
        return "liquid-retina"
    return None


def plan(laptops: Iterable[Any]) -> tuple[list[tuple[Any, str, str]], list[tuple[Any, str]]]:
    """(laptop, new code, new name) and (laptop, why not). Writes nothing."""
    laptops = list(laptops)
    pro = [l for l in laptops if l.model_code.startswith("apple-macbook-pro-")]
    writes, skips = [], []
    for laptop in pro:
        line = line_of(laptop)
        if line is None:
            skips.append((laptop, f"line unknown: display_type {laptop.display_type!r}"))
            continue
        code = with_line_token(laptop.model_code, line)
        name = build_name("MacBook Pro", line, laptop.display_size_inch, laptop.processor_model,
                          laptop.cpu_cores, laptop.gpu_cores, laptop.ram_gb, laptop.ssd_gb)
        if code == laptop.model_code and name == laptop.product_name:
            skips.append((laptop, "already done"))
        elif code == laptop.model_code and line not in code:
            skips.append((laptop, f"code has no place for the token: {code}"))
        else:
            writes.append((laptop, code, name))

    # The unique index: a new code must not be one another row holds or will hold.
    kept = {l.model_code for l in laptops} - {l.model_code for l, c, _ in writes if c != l.model_code}
    new_codes = [c for _, c, _ in writes]
    clashes = {c for c in new_codes if c in kept or new_codes.count(c) > 1}
    skips += [(l, f"collides with an existing code: {c}") for l, c, _ in writes if c in clashes]
    return [w for w in writes if w[1] not in clashes], skips


def apply(connection: Connection, writes: list[tuple[Any, str, str]]) -> int:
    """Rows changed. The caller owns the transaction, so a failure writes nothing."""
    return sum(
        connection.execute(
            update(_LAPTOPS)
            .where(_LAPTOPS.c.id == laptop.id, _LAPTOPS.c.model_code == laptop.model_code)
            .values(model_code=code, product_name=name)
        ).rowcount
        for laptop, code, name in writes
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("APPLE_BACKFILL_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")

    engine = create_engine(url)
    with engine.begin() as connection:
        if not args.apply:
            connection.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")  # this one, already open
        writes, skips = plan(read_laptops(connection))
        for laptop, code, name in writes:
            print(f"WRITE {laptop.id} [{laptop.status}]\n  {laptop.model_code} -> {code}\n"
                  f"  {laptop.product_name} -> {name}")
        for laptop, reason in skips:
            print(f"SKIP  {laptop.id} [{laptop.status}] {reason}")
        changed = apply(connection, writes) if args.apply else 0
    engine.dispose()

    print(f"{len(writes)} to write, {len(skips)} skipped, {changed} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
