"""
Backfill laptops.price_checked_at ("Last Checked") from price history.

Source: the laptop's latest laptop_price_history row, and only when that row's
price equals the current price_rm -- that row is a moment a scrape or an admin
set exactly this price. It is a LOWER BOUND: a later re-process that confirmed
the same price left no trace anywhere (processor job results name raw records,
not model codes), so the true last check may be newer, never older.

Left NULL, on purpose (no trustworthy date, so the page shows none):
  - price_rm is 0 ("price unknown": nothing to have checked)
  - no history row (the price got there some way that left no record)
  - no row at the latest history timestamp has the current price (the current
    price's date is unknown)

Only rows with price_checked_at IS NULL are written, so re-running after
deploy never overwrites a real check. Read-only unless --apply. Works before
the c3f7a9e2d5b1 migration is applied (dry run only; --apply refuses).

Usage:
    python -m app.scripts.backfill_price_checked
    python -m app.scripts.backfill_price_checked --apply
"""
import argparse
import sys

from sqlalchemy import text

from app.database import engine

_LATEST = """
    with m as (
        select laptop_id, max(recorded_at) as recorded_at from laptop_price_history group by laptop_id
    ), h as (
        -- every row at the latest timestamp: some laptops have two rows written
        -- in the same instant, and neither is "the" latest
        select m.laptop_id, m.recorded_at, array_agg(ph.price_rm) as hist_prices
        from m join laptop_price_history ph
          on ph.laptop_id = m.laptop_id and ph.recorded_at = m.recorded_at
        group by m.laptop_id, m.recorded_at
    )
    select l.id, l.status, l.price_rm, h.hist_prices, h.recorded_at, {checked} as checked
    from laptops l left join h on h.laptop_id = l.id
"""


def _bucket(row) -> str:
    if row.checked is not None:
        return "already set (left alone)"
    if not row.price_rm or row.price_rm <= 0:
        return "NULL: no price"
    if row.recorded_at is None:
        return "NULL: priced, no history row"
    if row.price_rm not in row.hist_prices:
        return "NULL: latest history != current price"
    return "set " + row.recorded_at.strftime("%Y-%m")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the dates (default: dry run)")
    args = parser.parse_args()

    with engine.connect() as conn:
        has_column = conn.execute(text(
            "select 1 from information_schema.columns "
            "where table_name = 'laptops' and column_name = 'price_checked_at'"
        )).first() is not None
        if args.apply and not has_column:
            print("laptops.price_checked_at does not exist yet: apply migration c3f7a9e2d5b1 first.")
            return 1
        rows = conn.execute(text(_LATEST.format(checked="l.price_checked_at" if has_column else "null"))).all()

    counts: dict[tuple[str, str], int] = {}
    to_set = []
    for row in rows:
        bucket = _bucket(row)
        counts[(bucket, row.status)] = counts.get((bucket, row.status), 0) + 1
        if bucket.startswith("set "):
            to_set.append({"id": row.id, "at": row.recorded_at})

    print(f"{'DRY RUN' if not args.apply else 'APPLY'} -- {len(rows)} laptops"
          + ("" if has_column else " (column not migrated yet)"))
    for (bucket, status), n in sorted(counts.items()):
        print(f"  {bucket:42} {status:10} {n:4}")
    print(f"  would set: {len(to_set)}" if not args.apply else f"  setting: {len(to_set)}")

    if args.apply and to_set:
        with engine.begin() as conn:
            conn.execute(
                # recorded_at is a naive UTC timestamp; say so, rather than let
                # the session time zone decide what it means.
                text("update laptops set price_checked_at = cast(:at as timestamp) at time zone 'UTC' "
                     "where id = :id and price_checked_at is null"),
                to_set,
            )
        print("  done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
