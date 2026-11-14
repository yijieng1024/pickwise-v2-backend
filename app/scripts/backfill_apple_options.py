"""
One-time backfill of Apple build-to-order options (step 10c, decided 2026-11-04).

    python -m app.scripts.backfill_apple_options            # fetches and prints, writes nothing
    python -m app.scripts.backfill_apple_options --apply    # one transaction, snapshots in backups/

Reads and writes TEST_DATABASE_URL unless APPLE_OPTIONS_TARGET=production is set
(app/common/db_target.py). Both modes call Apple: the plan is what Apple's configurator
says today (app/scraper/apple_options_fetch.py, the same code the buy page job runs),
a few seconds per call, and a 403, 429 or captcha stops the fetch.

--apply replaces the laptop_options rows of every chip kit that was fetched, and nothing
else: a failed kit keeps whatever it had. The snapshots hold the whole table (a few
hundred rows) before and after. Safe to run again.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlmodel import Session

from app.common.db_target import host_of, write_target
from app.laptops.option_model import LaptopOption
from app.scraper import apple_options_fetch as fetch

REPO = Path(__file__).resolve().parents[2]


def plan(session: Session, get: fetch.Get = fetch.http_get, pause_s: float = fetch.PAUSE_S) -> tuple[list, dict]:
    """Read the sizes on the stored buy pages, then fetch their chip kits. Ends the read transaction before any
    HTTP, so nothing idles open across the fetch. Writes nothing."""
    targets, targets_failed = fetch.read_targets(session)
    session.rollback()
    return targets_failed, fetch.fetch_all(targets, get, pause_s)


def _snapshot(session: Session, label: str, backups: Path) -> Path:
    rows = {"laptop_options": [dict(r._mapping) for r in session.execute(select(LaptopOption.__table__))]}
    path = backups / f"apple_options_{datetime.now():%Y-%m-%d_%H%M%S_%f}_{label}-backfill.json"
    path.write_text(json.dumps(rows, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path}")
    return path


def apply(session: Session, fetched: dict, backups: Path) -> None:
    _snapshot(session, "pre", backups)
    fetch.write(session, fetched["kits"])
    _snapshot(session, "post", backups)


def run(session: Session, write: bool, get: fetch.Get = fetch.http_get, pause_s: float = fetch.PAUSE_S,
        backups: Optional[Path] = None) -> dict:
    targets_failed, fetched = plan(session, get, pause_s)
    if write:
        apply(session, fetched, backups or REPO / "backups")
        session.commit()
    return fetch.summary(targets_failed, fetched)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("APPLE_OPTIONS_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")

    engine = create_engine(url)
    with Session(engine) as session:
        if not args.apply:
            session.connection().exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        result = run(session, args.apply)
    engine.dispose()

    print(json.dumps({k: v for k, v in result.items() if k not in ("labels_missing", "chip_not_offered")}, indent=2))
    for key in ("labels_missing", "chip_not_offered"):
        for entry in result[key]:
            print(f"{key}: {entry}")
    print("written" if args.apply else "nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
