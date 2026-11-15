"""
Fill `laptop_skus.last_seen_at` from one fetch of every ROG and HP page, so the first
weekly run does not list every SKU as "not seen since tracking started". One-off, after
migration b5e2c8f1a7d3. A SKU the fetch does not see is left null.

    python -m app.scripts.backfill_sku_last_seen            # fetches and prints, writes nothing
    python -m app.scripts.backfill_sku_last_seen --apply

Fetches with the re-scrape dry run's own fetch (about 10 minutes), the same pages a weekly
run re-scrapes. Reads and writes TEST_DATABASE_URL unless SKU_LAST_SEEN_TARGET=production
is set, and checks that the URL is the database it claims to be (app/common/db_target.py).
-W and -WP read as one SKU, as in the weekly check. Safe to run again.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import asyncio
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, select, update

from app.common.db_target import host_of, write_target
from app.laptops.laptop_models import LaptopSku
from app.laptops.price_guide import sku_key
from app.scripts import rescrape_dry_run as rescrape

_SKUS = LaptopSku.__table__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("SKU_LAST_SEEN_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")
    engine = create_engine(url)

    with engine.connect() as connection:
        targets, stored = rescrape.read_targets(connection), rescrape.read_stored(connection)
    # The fetch takes minutes: no connection is held across it.
    variants, failures = asyncio.run(rescrape.fetch(targets, stored))
    seen = {sku_key(v.sku) for v in variants if v.sku}
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        rows = connection.execute(select(_SKUS.c.id, _SKUS.c.sku)).all()
        hit = [r.id for r in rows if sku_key(r.sku) in seen]
        for r in rows:
            print(f"{'SEEN    ' if r.id in hit else 'NOT SEEN'} {r.sku}")
        if args.apply and hit:
            connection.execute(update(_SKUS).where(_SKUS.c.id.in_(hit)).values(last_seen_at=now))
    engine.dispose()

    print(f"{len(targets)} pages, {len(failures)} failed, {len(seen)} SKUs on them; "
          f"{len(hit)} of {len(rows)} laptop_skus rows seen, {len(hit) if args.apply else 0} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
