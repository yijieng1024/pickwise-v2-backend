"""
Round 3 of the 2026-11-03 MacBook Pro repair: each row becomes the machine its data
already describes, and the two configurations with no row get one.

    python -m app.scripts.repair_apple_round3            # prints, writes nothing
    python -m app.scripts.repair_apple_round3 --apply

Reads and writes TEST_DATABASE_URL unless APPLE_REPAIR_TARGET=production is set
(app/common/db_target.py). One transaction. Safe to run again: every step compares the
wanted value with the stored one, and a new row whose code exists is not inserted.

- c07480ce holds PROLED 24GB/2TB data under a Liquid Retina code → PROLED code and name,
  price 0, and its June (Liquid Retina) price history deleted, after a JSON copy in backups/.
- 5f0c1326 holds Liquid Retina 48GB/1TB data under a PROLED code → Liquid Retina code and
  name, suspended → inactive.
- New PROLED 48GB/1TB (copied from 857b4cbb) and Liquid Retina 24GB/2TB (from 431adb7e),
  price 0, inactive.
- Brightness is peak SDR: PROLED 2000, Liquid Retina 1000. processor_model takes the June
  form, "Apple M5 Pro (15-core)". An empty materials / accessories / warranty field on a
  Liquid Retina row is filled from e05aa3e6 as it was before round 3; PROLED rows are left
  empty and listed (no PROLED source for them).

An active row may only have brightness or processor_model changed; any other change to
one refuses the whole plan, as does a model_code collision. family_id, embeddings and
Pick Scores are not touched. Written through the ORM, so the mapper event stamps
updated_at: these are real data changes, and the Dashboard should call scores stale.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select
from sqlmodel import Session

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.processor.apple_name import build_name, chip_of, line_of_code, with_line_token

REPO = Path(__file__).resolve().parents[2]
PRE_ROUND3 = REPO / "backups" / "apple_laptops_2026-11-03_pre-round3.json"
DONOR_ID = "e05aa3e6-b3a9-4539-9c6d-0ad6a9e2f84f"
# The two rows whose code names the other line from their data.
RELINE = {
    "c07480ce-bf29-4eaa-97c0-153faf622565": "proled",
    "5f0c1326-7403-413b-8c0f-553e6a7f88d1": "liquid-retina",
}
CLEAR_PRICE = "c07480ce-bf29-4eaa-97c0-153faf622565"
UNSUSPEND = "5f0c1326-7403-413b-8c0f-553e6a7f88d1"
# (copy this row, with this storage): the configurations with no row.
NEW_ROWS = [
    ("857b4cbb-85d8-4e0c-8135-46e1a9df6503", 1024),
    ("431adb7e-fada-494b-bd36-19f719b6b8e8", 2048),
]
# Price history from before the PROLED page existed is the June Liquid Retina machine's.
SPLIT_AT = datetime(2026, 11, 3, 6, 52)
BRIGHTNESS = {"proled": 2000, "liquid-retina": 1000}
FILL = ("materials_and_certifications", "bundled_accessories", "warranty_details")
ACTIVE_MAY_CHANGE = {"display_brightness_nits", "processor_model"}
_NOT_COPIED = {"id", "created_at", "updated_at", "price_checked_at", "manufacturer_sku"}


class Refused(RuntimeError):
    """The plan breaks a rule; nothing is written."""


def _naive(moment) -> datetime:
    moment = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _retoken(code: str, line: str) -> str:
    old = line_of_code(code)
    return with_line_token(code.replace(f"-{old}-", "-", 1) if old else code, line)


def _processor(row: Mapping) -> Optional[str]:
    chip = chip_of(row["processor_model"])
    return f"Apple {chip} ({row['cpu_cores']}-core)" if chip and row["cpu_cores"] else row["processor_model"]


def _wanted(row: Mapping, donor: Mapping) -> dict:
    """The row as it should be. Values only; the caller diffs."""
    want = dict(row)
    line = RELINE.get(str(row["id"])) or line_of_code(row["model_code"])
    want["model_code"] = _retoken(row["model_code"], line)
    want["processor_model"] = _processor(row)
    want["display_brightness_nits"] = BRIGHTNESS[line]
    if str(row["id"]) == CLEAR_PRICE:
        want["price_rm"] = 0.0
    if str(row["id"]) == UNSUSPEND and row["status"] == "suspended":
        want["status"] = "inactive"
    if line == "liquid-retina":
        for field in FILL:
            if not row[field]:
                want[field] = donor[field]
    want["product_name"] = build_name(
        "MacBook Pro", line, want["display_size_inch"], want["processor_model"],
        want["cpu_cores"], want["gpu_cores"], want["ram_gb"], want["ssd_gb"],
    )
    return want


def plan(rows: Iterable[Mapping], donor: Mapping, history: Iterable[Mapping] = ()) -> dict:
    """
    {"updates": [(row, {field: (old, new)})], "inserts": [(source id, fields)],
     "delete_history": [history rows], "empty_proled": [(row id, fields)]}.
    Raises Refused on a collision or a forbidden change to an active row.
    """
    rows = [dict(r) for r in rows]
    pro = [r for r in rows if r["model_code"].startswith("apple-macbook-pro-")]
    wanted = {str(r["id"]): _wanted(r, donor) for r in pro}

    updates, empty_proled = [], []
    for row in pro:
        want = wanted[str(row["id"])]
        diff = {k: (row[k], want[k]) for k in want if want[k] != row[k]}
        if diff:
            updates.append((row, diff))
        if line_of_code(want["model_code"]) == "proled":
            missing = [f for f in FILL if not want[f]]
            if missing:
                empty_proled.append((str(row["id"]), missing))
        if row["status"] == "active" and set(diff) - ACTIVE_MAY_CHANGE:
            raise Refused(f"active row {row['id']} would change {sorted(set(diff) - ACTIVE_MAY_CHANGE)}")

    codes = [w["model_code"] for w in wanted.values()] + [
        r["model_code"] for r in rows if r not in pro
    ]
    inserts = []
    for source_id, ssd in NEW_ROWS:
        source = wanted.get(source_id)
        if source is None:
            raise Refused(f"source row {source_id} not found")
        code = source["model_code"].replace(f"-{source['ssd_gb']}gb", f"-{ssd}gb")
        if any(w["model_code"] == code for w in wanted.values()):
            continue  # inserted by an earlier run
        fields = {k: v for k, v in source.items() if k not in _NOT_COPIED}
        fields.update(ssd_gb=ssd, model_code=code, price_rm=0.0, status="inactive")
        fields["product_name"] = build_name(
            "MacBook Pro", line_of_code(code), fields["display_size_inch"], fields["processor_model"],
            fields["cpu_cores"], fields["gpu_cores"], fields["ram_gb"], ssd,
        )
        inserts.append((source_id, fields))
        codes.append(code)

    clashes = sorted({c for c in codes if codes.count(c) > 1})
    if clashes:
        raise Refused(f"model_code collision: {clashes}")

    old_history = [
        h for h in history
        if str(h["laptop_id"]) == CLEAR_PRICE and _naive(h["recorded_at"]) < SPLIT_AT
    ]
    return {"updates": updates, "inserts": inserts, "delete_history": old_history, "empty_proled": empty_proled}


def _read(session: Session) -> tuple[list[dict], list[dict]]:
    table = Laptop.__table__
    rows = [dict(r._mapping) for r in session.execute(
        select(table).join(LaptopBrand.__table__, LaptopBrand.__table__.c.id == table.c.brand_id)
        .where(func.lower(LaptopBrand.__table__.c.name) == "apple")
        .order_by(table.c.created_at, table.c.model_code)
    )]
    hist = LaptopPriceHistory.__table__
    history = [dict(r._mapping) for r in session.execute(select(hist).where(hist.c.laptop_id == CLEAR_PRICE))]
    return rows, history


def _donor() -> dict:
    pre = json.loads(PRE_ROUND3.read_text(encoding="utf-8"))
    return next(r for r in pre if r["id"] == DONOR_ID)


def apply(session: Session, result: dict) -> None:
    """Renames first and flushed, so a freed code can be taken by an insert."""
    if result["delete_history"]:
        path = REPO / "backups" / f"c07480ce_price_history_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(result["delete_history"], default=str, indent=2), encoding="utf-8")
        print(f"price history saved to {path}")
        for h in result["delete_history"]:
            session.delete(session.get(LaptopPriceHistory, h["id"]))
    for row, diff in result["updates"]:
        laptop = session.get(Laptop, row["id"])
        for field, (_, new) in diff.items():
            setattr(laptop, field, new)
        session.add(laptop)
    session.flush()
    for _, fields in result["inserts"]:
        session.add(Laptop(**fields))
    session.flush()


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
        rows, history = _read(session)
        result = plan(rows, _donor(), history)
        for row, diff in result["updates"]:
            flag = " ACTIVE" if row["status"] == "active" else ""
            print(f"UPDATE {row['id']} [{row['status']}]{flag}")
            for field, (old, new) in diff.items():
                print(f"  {field}: {str(old)[:90]!r} -> {str(new)[:90]!r}")
        for source_id, fields in result["inserts"]:
            print(f"INSERT copy of {source_id[:8]}: {fields['model_code']} | {fields['product_name']} | "
                  f"{fields['status']} RM{fields['price_rm']} family {fields['family_id']}")
        for h in result["delete_history"]:
            print(f"DELETE price history {h['id']} RM{h['price_rm']} {h['recorded_at']}")
        for laptop_id, fields in result["empty_proled"]:
            print(f"EMPTY  PROLED {laptop_id[:8]}: {', '.join(fields)}")
        if args.apply:
            apply(session, result)
            session.commit()
        else:
            session.rollback()
    engine.dispose()

    print(f"{len(result['updates'])} updates, {len(result['inserts'])} inserts, "
          f"{len(result['delete_history'])} history rows to delete, "
          f"{'written' if args.apply else 'nothing written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
