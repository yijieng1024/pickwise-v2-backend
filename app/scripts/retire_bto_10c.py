"""
10c step (f), decided 2026-11-04/05: retire the 10 build-to-order Mac rows.

    python -m app.scripts.retire_bto_10c            # prints, writes nothing
    python -m app.scripts.retire_bto_10c --apply    # one transaction, snapshots in backups/

Each row is a standard config plus options (ADR-0009 Amendment II's "duplicates"), so it
goes to `suspended`. Status only: embeddings, Pick Scores and price history stay, as on
every other non-active row. a3205f6d's review chunks and links are re-pointed to the
standard config with exactly its chip (same family, CPU and GPU strings and core counts).

Reads and writes TEST_DATABASE_URL unless APPLE_REPAIR_TARGET=production is set
(app/common/db_target.py). Refused, nothing written: an id is not found or matches more
than one row, a status is not the expected one, the re-point target is not exactly one
active standard config with the same chip (or not the surveyed f3ebcce1), or a link
would collide with one the target already has.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import load_dotenv
from sqlalchemy import create_engine, select, update
from sqlmodel import Session

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand  # noqa: F401 - laptops.brand_id resolves on flush
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.reviews.link_model import ReviewLaptopLink
from app.reviews.models import LaptopReviewChunk

REPO = Path(__file__).resolve().parents[2]
TARGET = LaptopStatus.SUSPENDED.value
# id prefix -> status it must have now
EXPECTED = {
    **{k: "active" for k in ("e05aa3e6", "a3205f6d", "9d521cf2")},
    **{k: "inactive" for k in ("f24b8f91", "5f0c1326", "c07480ce", "ea76f7bb", "4eeaaa67", "857b4cbb", "e60560b4")},
}
REVIEWED = "a3205f6d"           # the one row with reviews
SURVEYED_TARGET = "f3ebcce1"    # the survey's standard config with the same chip
CHIP = ("processor_model", "gpu_model", "cpu_cores", "gpu_cores")


class Refused(RuntimeError):
    """The plan breaks a rule; nothing is written."""


def plan(rows: Iterable[Mapping], links: Iterable[Mapping], chunks: int) -> dict:
    """Pure. rows: every laptop of the retired rows' families (id, product_name, status,
    family_id, has_part_numbers and the CHIP fields). links: review_laptop_link rows
    (raw_review_id, family_id, laptop_id) of REVIEWED and its target. chunks: REVIEWED's
    chunk count."""
    rows, links = list(rows), list(links)
    status, problems = [], []
    found = {}
    for prefix, expected in EXPECTED.items():
        hits = [r for r in rows if str(r["id"]).startswith(prefix)]
        if len(hits) != 1:
            problems.append(f"{prefix}: {len(hits)} rows match")
            continue
        [row] = hits
        found[prefix] = row
        if row["status"] != expected:
            problems.append(f"{prefix}: status is {row['status']!r}, expected {expected!r}")
            continue
        status.append({"id": str(row["id"]), "product_name": row["product_name"],
                       "before": row["status"], "after": TARGET})

    repoint = None
    if REVIEWED in found:
        src = found[REVIEWED]
        same_chip = [r for r in rows if r["id"] != src["id"] and r["family_id"] == src["family_id"]
                     and r["status"] == LaptopStatus.ACTIVE.value and r["has_part_numbers"]
                     and all(r[f] == src[f] for f in CHIP)]
        if len(same_chip) != 1:
            problems.append(f"{REVIEWED}: {len(same_chip)} active standard configs with the same chip")
        elif not str(same_chip[0]["id"]).startswith(SURVEYED_TARGET):
            problems.append(f"{REVIEWED}: same-chip config is {str(same_chip[0]['id'])[:8]}, not {SURVEYED_TARGET}")
        else:
            [dst] = same_chip
            moved = [l for l in links if l["laptop_id"] == src["id"]]
            taken = {(l["raw_review_id"], l["family_id"]) for l in links if l["laptop_id"] == dst["id"]}
            if clash := [l for l in moved if (l["raw_review_id"], l["family_id"]) in taken]:
                problems.append(f"{REVIEWED}: {len(clash)} links already exist on {SURVEYED_TARGET}")
            repoint = {"from": str(src["id"]), "to": str(dst["id"]), "to_name": dst["product_name"],
                       "chip": tuple(src[f] for f in CHIP), "chunks": chunks, "links": len(moved)}
    if problems:
        raise Refused("; ".join(problems))
    return {"status": status, "repoint": repoint}


def _read(session: Session) -> tuple[list[dict], list[dict], int]:
    cols = (Laptop.id, Laptop.product_name, Laptop.status, Laptop.family_id, Laptop.part_numbers,
            *(getattr(Laptop, f) for f in CHIP))
    mine = [dict(r._mapping) for r in session.execute(select(*cols))
            if str(r.id)[:8] in EXPECTED]
    families = {r["family_id"] for r in mine if r["family_id"]}
    rows = {r["id"]: r for r in mine}
    for r in session.execute(select(*cols).where(Laptop.family_id.in_(families))):  # type: ignore[union-attr]
        rows[r.id] = dict(r._mapping)
    for r in rows.values():
        r["has_part_numbers"] = bool(r.pop("part_numbers"))
    ids = [i for i in rows if str(i).startswith((REVIEWED, SURVEYED_TARGET))]
    links = [dict(r._mapping) for r in session.execute(
        select(ReviewLaptopLink.raw_review_id, ReviewLaptopLink.family_id, ReviewLaptopLink.laptop_id)
        .where(ReviewLaptopLink.laptop_id.in_(ids)))]  # type: ignore[union-attr]
    src = next((i for i in rows if str(i).startswith(REVIEWED)), None)
    chunks = len(session.execute(select(LaptopReviewChunk.id).where(LaptopReviewChunk.laptop_id == src)).all())
    return list(rows.values()), links, chunks


def _snapshot(session: Session, result: dict, label: str, backups: Path) -> Path:
    ids = [e["id"] for e in result["status"]]
    rp = result["repoint"]
    reviewed = [rp["from"], rp["to"]] if rp else []

    def dump(table, column, values):
        return [dict(r._mapping) for r in session.execute(select(table).where(column.in_(values)))]

    data = {"laptops": dump(Laptop.__table__, Laptop.__table__.c.id, ids),
            "laptop_review_chunks": [{"id": r.id, "laptop_id": r.laptop_id} for r in session.execute(
                select(LaptopReviewChunk.id, LaptopReviewChunk.laptop_id)
                .where(LaptopReviewChunk.laptop_id.in_(reviewed)))],  # type: ignore[union-attr]
            "review_laptop_link": dump(ReviewLaptopLink.__table__, ReviewLaptopLink.__table__.c.laptop_id, reviewed)}
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / f"retire_bto_10c_{datetime.now():%Y-%m-%d_%H%M%S_%f}_{label}.json"
    path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path}")
    return path


def run(session: Session, write: bool, backups: Path = REPO / "backups") -> dict:
    result = plan(*_read(session))
    if not write:
        return result
    _snapshot(session, result, "pre", backups)
    for e in result["status"]:
        laptop = session.get(Laptop, e["id"])
        laptop.status = TARGET   # through the ORM: the mapper event stamps updated_at
        session.add(laptop)
    if rp := result["repoint"]:
        session.execute(update(LaptopReviewChunk).where(LaptopReviewChunk.laptop_id == rp["from"])
                        .values(laptop_id=rp["to"]))
        session.execute(update(ReviewLaptopLink).where(ReviewLaptopLink.laptop_id == rp["from"])
                        .values(laptop_id=rp["to"]))
    session.flush()
    _snapshot(session, result, "post", backups)
    session.commit()
    return result


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
        result = run(session, write=args.apply)
        for e in result["status"]:
            print(f"{e['id'][:8]}  {e['before']:>8} -> {e['after']}  {e['product_name'][:80]}")
        if rp := result["repoint"]:
            print(f"re-point {rp['from'][:8]} -> {rp['to'][:8]} ({rp['to_name']}): "
                  f"{rp['chunks']} chunks, {rp['links']} links; chip {rp['chip']}")
        if not args.apply:
            session.rollback()
    engine.dispose()
    print(f"{len(result['status'])} rows, {'written' if args.apply else 'nothing written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
