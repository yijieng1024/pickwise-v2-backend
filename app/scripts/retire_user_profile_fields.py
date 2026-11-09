"""
Onboarding, decided 2026-11-09: retire users.birthday, users.gender and
users.occupation. They were collected on the profile and read by nothing (the
Privacy page's claim that they help Pico was false); occupation is replaced by
user_background.user_type. The frontend stopped showing and sending them in
48a2357. This script empties them; a later migration drops the columns and
refuses while any value remains.

    python -m app.scripts.retire_user_profile_fields            # prints, writes nothing
    python -m app.scripts.retire_user_profile_fields --apply    # one transaction, snapshots in backups/

Reads and writes TEST_DATABASE_URL unless DATA_FIX_TARGET=production is set
(app/common/db_target.py). Rolled back, exit 1: the update touched a different
number of rows than planned, or any value is still set afterwards.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlmodel import Session

from app.common.db_target import host_of, write_target

REPO = Path(__file__).resolve().parents[2]
FIELDS = ("birthday", "gender", "occupation")


def plan(rows: list[dict]) -> dict:
    """Pure. rows: every user's id and the three fields. Returns the count of
    non-null values per field and the rows that have any (the ones to snapshot
    and clear)."""
    affected = [r for r in rows if any(r[f] is not None for f in FIELDS)]
    return {"users": len(rows),
            "counts": {f: sum(r[f] is not None for r in rows) for f in FIELDS},
            "affected": affected}


def _rows(session: Session) -> list[dict]:
    return [dict(r._mapping) for r in session.execute(text(
        "select id, birthday, gender, occupation from users order by id"))]


def _snapshot(rows: list[dict], label: str, backups: Path) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / f"retire_user_profile_fields_{datetime.now():%Y-%m-%d_%H%M%S_%f}_{label}.json"
    path.write_text(json.dumps(rows, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path} ({len(rows)} users)")
    return path


def run(session: Session, write: bool, backups: Path = REPO / "backups") -> dict:
    result = plan(_rows(session))
    if not write:
        return result
    ids = [r["id"] for r in result["affected"]]
    _snapshot(result["affected"], "pre", backups)
    cleared = session.execute(text(
        "update users set birthday = null, gender = null, occupation = null where id = any(:ids)"),
        {"ids": ids}).rowcount
    left = plan(_rows(session))
    if cleared != len(ids) or left["affected"]:
        session.rollback()
        raise RuntimeError(f"rolled back: cleared {cleared} of {len(ids)} users, "
                           f"{len(left['affected'])} still have a value")
    session.commit()
    _snapshot([r for r in _rows(session) if r["id"] in set(ids)], "post", backups)
    return {**result, "cleared": cleared}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()
    load_dotenv()
    url = write_target("DATA_FIX_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            if not args.apply:
                session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
            try:
                result = run(session, write=args.apply)
            except RuntimeError as e:
                print(f"REFUSED: {e}")
                return 1
            print(f"users: {result['users']}")
            for field, n in result["counts"].items():
                print(f"  {field:<11} non-null: {n}")
            print(f"users with any value: {len(result['affected'])}")
            if args.apply:
                print(f"cleared {result['cleared']} users")
            else:
                session.rollback()
                print("nothing written")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
