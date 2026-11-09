"""
Onboarding commit 2, decided 2026-11-09: move the questionnaire's internal notes out
of help_text (which the wizard shows to users) into admin_note, and give each of the
six laptop questions user-facing help text.

    python -m app.scripts.questionnaire_help_text            # prints, writes nothing
    python -m app.scripts.questionnaire_help_text --apply    # one transaction, snapshots in backups/

Reads and writes TEST_DATABASE_URL unless DATA_FIX_TARGET=production is set
(app/common/db_target.py). Refused, nothing written: the admin_note column does not
exist yet (--apply only; deploy migration e6a2c9f4b8d1 first), the laptop questions are
not exactly the six target fields below, a row already has an admin_note, or a row's
help_text is not an internal note (it does not start with "To know"), which is what a
second run, or an admin who already rewrote it, looks like.
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
NEW_HELP = {
    "budget": "Pick the range you're comfortable spending. We'll still show close options.",
    "purpose": "Choose what you'll do most often. It decides which parts of the laptop matter most.",
    "priorities": "Put what matters most to you first.",
    "screen_size": "Smaller is easier to carry; bigger is easier on the eyes.",
    "portability": "Say yes if you'll carry it most days. Lighter laptops often give up some performance.",
    "brand_preferences": "Pick a brand you prefer, or choose no preference to see every brand.",
}
OLD_NOTE_PREFIX = "To know"


class Refused(RuntimeError):
    """A precondition failed; nothing is written."""


def _has_admin_note(session: Session) -> bool:
    return bool(session.execute(text(
        "select 1 from information_schema.columns "
        "where table_name = 'questionnaire_questions' and column_name = 'admin_note'")).first())


def _rows(session: Session, with_note: bool) -> list[dict]:
    note = "q.admin_note" if with_note else "null as admin_note"
    return [dict(r._mapping) for r in session.execute(text(f"""
        select q.id, q.step_order, q.target_field, q.question_text, q.help_text, {note}
        from questionnaire_questions q join product_types p on p.id = q.product_type_id
        where p.name = 'laptop' and q.is_active order by q.step_order"""))]


def plan(rows: list[dict]) -> list[dict]:
    """Pure. One entry per row: the current help_text becomes admin_note, NEW_HELP the help_text."""
    fields = sorted(r["target_field"] for r in rows)
    if fields != sorted(NEW_HELP):
        raise Refused(f"active laptop questions are {fields}, expected exactly {sorted(NEW_HELP)}")
    problems = [f"{r['target_field']}: admin_note already set" for r in rows if r["admin_note"]]
    problems += [f"{r['target_field']}: help_text is not an internal note ({(r['help_text'] or '')[:40]!r})"
                 for r in rows if not (r["help_text"] or "").startswith(OLD_NOTE_PREFIX)]
    if problems:
        raise Refused("; ".join(problems))
    return [{"id": str(r["id"]), "step_order": r["step_order"], "target_field": r["target_field"],
             "question_text": r["question_text"], "help_text_now": r["help_text"],
             "admin_note": r["help_text"], "help_text_new": NEW_HELP[r["target_field"]]} for r in rows]


def _snapshot(session: Session, label: str, backups: Path) -> Path:
    data = [dict(r._mapping) for r in session.execute(text(
        "select * from questionnaire_questions order by step_order"))]
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / f"questionnaire_help_text_{datetime.now():%Y-%m-%d_%H%M%S_%f}_{label}.json"
    path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path} ({len(data)} rows)")
    return path


def run(session: Session, write: bool, backups: Path = REPO / "backups") -> list[dict]:
    has_note = _has_admin_note(session)
    if write and not has_note:
        raise Refused("questionnaire_questions.admin_note does not exist: deploy migration e6a2c9f4b8d1 first")
    entries = plan(_rows(session, has_note))
    if not write:
        return entries
    _snapshot(session, "pre", backups)
    for e in entries:
        session.execute(text("update questionnaire_questions set admin_note = :note, help_text = :help where id = :id"),
                        {"note": e["admin_note"], "help": e["help_text_new"], "id": e["id"]})
    session.commit()
    _snapshot(session, "post", backups)
    return entries


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
                if not _has_admin_note(session):
                    print("(admin_note column not migrated yet; read as empty)")
            try:
                entries = run(session, write=args.apply)
            except Refused as e:
                print(f"REFUSED: {e}")
                return 1
            for e in entries:
                print(f"\n{e['step_order']}. {e['question_text']}  [{e['target_field']}]")
                print(f"   help_text now  : {e['help_text_now']}")
                print(f"   -> admin_note  : {e['admin_note']}")
                print(f"   -> help_text   : {e['help_text_new']}")
            if not args.apply:
                session.rollback()
            print(f"\n{len(entries)} rows, {'written' if args.apply else 'nothing written'}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
