"""
10b step (f), decided 2026-11-04: move 12 rows to `discontinued` (ADR-0009 Amendment II).

    python -m app.scripts.discontinue_10b            # prints, writes nothing
    python -m app.scripts.discontinue_10b --apply    # one transaction, snapshot in backups/

Reads and writes TEST_DATABASE_URL unless APPLE_REPAIR_TARGET=production is set
(app/common/db_target.py). Changes `status` only. Refused, nothing written: an id is
not found or matches more than one row, or a row's status is not the expected one.
Written through the ORM, so the mapper event stamps updated_at.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlmodel import Session

from app.common.db_target import host_of, write_target
from app.laptops.laptop_models import Laptop, LaptopPriceHistory, LaptopStatus, last_official_price

REPO = Path(__file__).resolve().parents[2]
TARGET = LaptopStatus.DISCONTINUED.value
# id prefix -> status it must have now
EXPECTED = {
    **{k: "active" for k in ("0670a4d3", "7bee501c", "bf390849")},  # old M5 Macs
    **{k: "suspended" for k in ("128dcb6b", "63b4c73c", "54d2b621", "e6860697", "d09e2141",
                                "cd3c6c19", "8199745e", "8da2d5ec", "7d7e74c8")},
}


class Refused(RuntimeError):
    """The plan breaks a rule; nothing is written."""


def plan(rows: Iterable[Mapping]) -> list[dict]:
    """Pure. rows: id, product_name, status, price_rm, price_checked_at, history
    ((price_rm, recorded_at, source) tuples). One entry per EXPECTED id."""
    rows = list(rows)
    out, problems = [], []
    for prefix, expected in EXPECTED.items():
        hits = [r for r in rows if str(r["id"]).startswith(prefix)]
        if len(hits) != 1:
            problems.append(f"{prefix}: {len(hits)} rows match")
            continue
        [row] = hits
        if row["status"] != expected:
            problems.append(f"{prefix}: status is {row['status']!r}, expected {expected!r}")
            continue
        price, at = last_official_price(row["price_rm"], row["price_checked_at"], row["history"])
        out.append({"id": str(row["id"]), "product_name": row["product_name"], "before": row["status"],
                    "after": TARGET, "last_official": (price, at)})
    if problems:
        raise Refused("; ".join(problems))
    return out


def _read(session: Session) -> list[dict]:
    rows = [dict(r._mapping) for r in session.execute(
        select(Laptop.id, Laptop.product_name, Laptop.status, Laptop.price_rm, Laptop.price_checked_at))]
    rows = [r for r in rows if str(r["id"])[:8] in EXPECTED]
    for r in rows:
        r["history"] = [tuple(h) for h in session.execute(
            select(LaptopPriceHistory.price_rm, LaptopPriceHistory.recorded_at, LaptopPriceHistory.source)
            .where(LaptopPriceHistory.laptop_id == r["id"]))]
    return rows


def _snapshot(session: Session, ids: list[str], label: str) -> None:
    table = Laptop.__table__
    rows = [dict(r._mapping) for r in session.execute(select(table).where(table.c.id.in_(ids)))]
    path = REPO / "backups" / f"laptops_{datetime.now():%Y-%m-%d_%H%M%S}_{label}-discontinue-10b.json"
    path.write_text(json.dumps(rows, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()
    load_dotenv()
    url = write_target("APPLE_REPAIR_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")
    engine = create_engine(url)
    with Session(engine) as session:
        if not args.apply:
            connection = session.connection()
            connection.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        result = plan(_read(session))
        for e in result:
            price, at = e["last_official"]
            shown = f"RM {price:,.0f} ({at:%d %b %Y})" if price else "No recorded price"
            print(f"{e['id'][:8]}  {e['before']:>9} -> {e['after']}  {shown}  {e['product_name'][:70]}")
        if args.apply:
            ids = [e["id"] for e in result]
            _snapshot(session, ids, "pre")
            for e in result:
                laptop = session.get(Laptop, e["id"])
                laptop.status = TARGET
                session.add(laptop)
            session.flush()
            _snapshot(session, ids, "post")
            session.commit()
        else:
            session.rollback()
    engine.dispose()
    print(f"{len(result)} rows, {'written' if args.apply else 'nothing written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
