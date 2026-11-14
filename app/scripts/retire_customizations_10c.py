"""
10c step (g)(a), decided 2026-11-09: delete every laptop_customizations row and the 6
Apple category rows, before step (b)'s migration drops the table.

    python -m app.scripts.retire_customizations_10c            # prints, writes nothing
    python -m app.scripts.retire_customizations_10c --apply    # one transaction, snapshots in backups/

The Configure flow reads laptop_options, never these. The 6 categories are the old
customization groups; they have no laptop links, but they are active, so the processor
offers them as tags. The JSON snapshots in backups/ are the only copy kept.

Reads and writes TEST_DATABASE_URL unless APPLE_REPAIR_TARGET=production is set
(app/common/db_target.py). Refused, nothing written: the category rows named below are
not exactly the 6 (one missing or duplicated), or any of them is linked to a laptop.
Rolled back, exit 1: after the deletes, anything other than those rows changed (the
protected categories, their links, or the totals).
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
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
APPLE = ("storage", "memory", "power_adapter", "apple_care", "display_type", "processor")
PROTECTED = ("Business", "Creator", "Gaming", "Workstation")


class Refused(RuntimeError):
    """A precondition failed; nothing is written."""


def _rows(session: Session, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in session.execute(text(sql), params)]


def _categories(session: Session, names) -> list[dict]:
    return _rows(session, """
        select c.*, (select count(*) from laptop_categories x where x.category_id = c.id) as links
        from categories c where c.name = any(:names) order by c.name""", names=list(names))


def _state(session: Session) -> dict:
    """Everything that must not change except the target rows."""
    return {
        "protected": _categories(session, PROTECTED),
        "protected_links": _rows(session, """
            select x.laptop_id, x.category_id from laptop_categories x
            join categories c on c.id = x.category_id where c.name = any(:names)
            order by 1, 2""", names=list(PROTECTED)),
        "categories": session.execute(text("select count(*) from categories")).scalar_one(),
        "links": session.execute(text("select count(*) from laptop_categories")).scalar_one(),
    }


def survey(session: Session) -> dict:
    apple = _categories(session, APPLE)
    return {
        "customizations": session.execute(text("select count(*) from laptop_customizations")).scalar_one(),
        "by_status": _rows(session, """
            select l.status, count(*) as n from laptop_customizations lc
            join laptops l on l.id = lc.laptop_id group by 1 order by 1"""),
        "apple": apple,
        "protected": _categories(session, PROTECTED),
    }


def check(apple: list[dict]) -> None:
    names = sorted(r["name"] for r in apple)
    if names != sorted(APPLE):
        raise Refused(f"target categories are {names}, expected exactly {sorted(APPLE)}")
    if linked := [r["name"] for r in apple if r["links"]]:
        raise Refused(f"categories still linked to laptops: {linked}")


def _snapshot(session: Session, label: str, backups: Path) -> Path:
    data = {"laptop_customizations": _rows(session, "select * from laptop_customizations order by id"),
            "categories": _rows(session, "select * from categories where name = any(:n) order by name", n=list(APPLE))}
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / f"retire_customizations_10c_{datetime.now():%Y-%m-%d_%H%M%S_%f}_{label}.json"
    path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path} ({len(data['laptop_customizations'])} customizations, "
          f"{len(data['categories'])} categories)")
    return path


def run(session: Session, write: bool, backups: Path = REPO / "backups") -> dict:
    result = survey(session)
    check(result["apple"])
    if not write:
        return result
    _snapshot(session, "pre", backups)
    before = _state(session)
    deleted = session.execute(text("delete from laptop_customizations")).rowcount
    removed = session.execute(text("delete from categories where name = any(:n)"), {"n": list(APPLE)}).rowcount
    after = _state(session)
    expected = {**before, "categories": before["categories"] - len(APPLE)}
    left = session.execute(text("select count(*) from laptop_customizations")).scalar_one()
    if after != expected or deleted != result["customizations"] or removed != len(APPLE) or left:
        session.rollback()
        raise RuntimeError(f"rolled back: something besides the target rows changed "
                           f"(deleted {deleted} customizations, {removed} categories, {left} left)")
    session.commit()
    _snapshot(session, "post", backups)
    return {**result, "deleted": deleted, "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()
    load_dotenv()
    url = write_target("APPLE_REPAIR_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            if not args.apply:
                session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
            try:
                result = run(session, write=args.apply)
            except (Refused, RuntimeError) as e:
                print(f"REFUSED: {e}")
                return 1
            print(f"laptop_customizations: {result['customizations']} rows")
            for r in result["by_status"]:
                print(f"  laptop status {r['status']:<13} {r['n']:>4}")
            print(f"{'category':<14} {'is_active':<9} {'links':>5}   role")
            for role, rows in (("delete", result["apple"]), ("protected", result["protected"])):
                for r in rows:
                    print(f"{r['name']:<14} {str(r['is_active']):<9} {r['links']:>5}   {role}")
            if args.apply:
                print(f"deleted {result['deleted']} customizations and {result['removed']} categories")
            else:
                session.rollback()
                print("nothing written")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
