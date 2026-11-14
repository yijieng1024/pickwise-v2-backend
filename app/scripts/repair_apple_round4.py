"""
Round 4 of the Apple repair (10a, decided 2026-11-03/04): the fields the LLM got wrong,
corrected from the specs page text.

    python -m app.scripts.repair_apple_round4            # prints, writes nothing
    python -m app.scripts.repair_apple_round4 --apply    # one transaction, snapshot in backups/

Reads and writes TEST_DATABASE_URL unless APPLE_REPAIR_TARGET=production is set
(app/common/db_target.py). Safe to run again: a field is changed only when the page
says something different from what is stored.

1. Every Apple row: max_ram_gb, weight_kg, power_supply_details, external_display_support,
   display_resolution (and Neo fingerprint_reader) from app/processor/apple_fields.py, run
   on the newest stored raw text of the row's specs page. A field changes only when it
   parses and differs. A failed parse changes nothing and is listed.
2. Two LLM fields the parser does not own, each with the page lines it is taken from:
   abc2df6f's keyboard (Neo 256GB has no Touch ID) and 9d521cf2's media engine. Every
   source line must be on the page, or the plan is refused.
3. The 10 build-to-order rows (on no buy page) are skipped, except for the fields this
   round names for them: 9d521cf2's media engine, f24b8f91's and 5f0c1326's charger.

Refused, nothing written: a named row is missing, a source line is not on the page, or
the parser does not give 5c87abdd max RAM 36 and a 96W charger (the plan's check).
Written through the ORM, so the mapper event stamps updated_at.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select
from sqlmodel import Session

from app.common.db_target import host_of, write_target
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop
from app.processor import apple_fields
from app.processor.apple_name import SplitFailed, _tidy, chip_of, line_of_code, size_sections
from app.scraper.models import RawScrapLaptop

REPO = Path(__file__).resolve().parents[2]
_MY = "https://www.apple.com/my"
_PAGES = {  # (model_code prefix, line) -> (specs page, product)
    ("apple-macbook-pro-", "proled"): (f"{_MY}/macbook-pro/proled-xdr-display/specs/", "MacBook Pro"),
    ("apple-macbook-pro-", "liquid-retina"): (f"{_MY}/macbook-pro/liquid-retina-xdr-display/specs/", "MacBook Pro"),
    ("apple-macbook-air-", None): (f"{_MY}/macbook-air/specs/", "MacBook Air"),
    ("apple-macbook-neo-", None): (f"{_MY}/macbook-neo/specs/", "MacBook Neo"),
}

ABC2 = "abc2df6f-"
D9 = "9d521cf2-"
C5 = "5c87abdd-"
# Build-to-order: the Apple rows with no buy-page part numbers, other than the three
# old M5 rows going to discontinued (10b), as of backups/..._post-buypage.json.
BUILD_TO_ORDER = {"e05aa3e6-", "a3205f6d-", D9, "f24b8f91-", "5f0c1326-",
                  "c07480ce-", "ea76f7bb-", "4eeaaa67-", "857b4cbb-", "e60560b4-"}
# What round 4 does change on a build-to-order row.
BTO_FIELDS = {D9: {"media_engine_details"}, "f24b8f91-": {"power_supply_details"},
              "5f0c1326-": {"power_supply_details"}}
# LLM fields, worded as the row's siblings are, from these page lines.
LLM_FIXES = {
    ABC2: ("keyboard_touchpad_details", "Magic Keyboard, Multi-Touch trackpad",
           ["Magic Keyboard", "Not available, Touch ID",
            "Multi-Touch trackpad for precise cursor control and support for gestures"]),
    D9: ("media_engine_details",
         "Hardware-accelerated H.264, HEVC, ProRes, and ProRes RAW, Video decode engine, "
         "Two video encode engines, Two ProRes encode and decode engines, AV1 decode",
         ["Hardware-accelerated H.264, HEVC, ProRes, and ProRes RAW", "Video decode engine",
          "Two video encode engines", "Two ProRes encode and decode engines", "AV1 decode"]),
}
CHECK = {C5: {"max_ram_gb": 36, "power_supply_details": "96W USB-C Power Adapter, USB-C to MagSafe 3 Cable (2m)"}}


class Refused(RuntimeError):
    """The plan breaks a rule; nothing is written."""


def _key(row: Mapping) -> str:
    return str(row["id"])[:9]


def page_of(model_code: str) -> Optional[tuple[str, str]]:
    for (prefix, line), page in _PAGES.items():
        if model_code.startswith(prefix) and line_of_code(model_code) == line:
            return page
    return None


def config_of(row: Mapping) -> dict:
    return {"size": int(row["display_size_inch"] or 0), "chip": chip_of(row["processor_model"]),
            "cpu_cores": row["cpu_cores"], "gpu_cores": row["gpu_cores"],
            "ram_gb": row["ram_gb"], "ssd_gb": row["ssd_gb"]}


def _parsed(row: Mapping, texts: Mapping[str, str]) -> tuple[dict, list[dict], Optional[str]]:
    """The parser's fields for this row, its failures, and the section it read."""
    page = page_of(row["model_code"])
    if page is None or page[0] not in texts:
        return {}, [{"field": "*", "reason": f"no stored specs text for {row['model_code']}"}], None
    url, product = page
    try:
        sections = size_sections(texts[url], product)
    except SplitFailed as e:
        return {}, [{"field": "*", "reason": str(e)}], None
    section = apple_fields.section_for(sections, config_of(row)["size"])
    fields, failures = apple_fields.parse(section, config_of(row), product)
    return fields, failures, section


def plan(rows: Iterable[Mapping], texts: Mapping[str, str]) -> dict:
    """Pure. Raises Refused on a missing named row, a source line not on the page, or a
    failed 5c87abdd check."""
    rows = list(rows)
    keys = {_key(r) for r in rows}
    if missing := sorted((set(LLM_FIXES) | set(CHECK) | set(BTO_FIELDS)) - keys):
        raise Refused(f"named rows not found: {missing}")
    result = {"updates": [], "skipped": [], "parse_failures": []}
    for row in rows:
        key = _key(row)
        if key in BUILD_TO_ORDER and key not in BTO_FIELDS:
            result["skipped"].append({"id": str(row["id"]), "model_code": row["model_code"],
                                      "reason": "build-to-order, kept for now"})
            continue
        fields, failures, section = _parsed(row, texts)
        for expect_field, expected in CHECK.get(key, {}).items():
            if fields.get(expect_field) != expected:
                raise Refused(f"{key} {expect_field}: parser gives {fields.get(expect_field)!r}, expected {expected!r}")
        wanted = {f: v for f, v in fields.items() if v is not None}
        if key in LLM_FIXES:
            field, value, sources = LLM_FIXES[key]
            page_lines = set(_tidy(section or "").split("\n"))
            if absent := [s for s in sources if s not in page_lines]:
                raise Refused(f"{key} {field}: source lines not on the page: {absent}")
            wanted[field] = value
        allowed = BTO_FIELDS.get(key)
        if allowed is not None:
            wanted = {f: v for f, v in wanted.items() if f in allowed}
            failures = [f for f in failures if f["field"] in allowed]
        diff = {f: (row[f], v) for f, v in wanted.items() if row[f] != v}
        if failures:
            result["parse_failures"].append({"id": str(row["id"]), "model_code": row["model_code"],
                                             "failures": failures})
        if diff:
            result["updates"].append((dict(row), diff))
    return result


def _read(session: Session) -> tuple[list[dict], dict[str, str]]:
    table = Laptop.__table__
    rows = [dict(r._mapping) for r in session.execute(
        select(table).join(LaptopBrand.__table__, LaptopBrand.__table__.c.id == table.c.brand_id)
        .where(func.lower(LaptopBrand.__table__.c.name) == "apple")
        .order_by(table.c.model_code)
    )]
    texts = {}
    for url, _ in _PAGES.values():
        raw = session.execute(select(RawScrapLaptop).where(RawScrapLaptop.source_url == url)
                              .order_by(RawScrapLaptop.created_at.desc())).scalars().first()  # type: ignore[attr-defined]
        if raw is not None:
            features = (raw.raw_specs_dump or {}).get("scraped_features") or []
            texts[url] = "\n".join(f for f in features if isinstance(f, str))
    return rows, texts


def _snapshot(session: Session, ids: list[str], label: str) -> Path:
    table = Laptop.__table__
    rows = [dict(r._mapping) for r in session.execute(select(table).where(table.c.id.in_(ids)))]
    path = REPO / "backups" / f"apple_laptops_{datetime.now():%Y-%m-%d_%H%M%S}_{label}-round4.json"
    path.write_text(json.dumps(rows, default=str, indent=2), encoding="utf-8")
    print(f"snapshot: {path}")
    return path


def apply(session: Session, result: dict) -> None:
    ids = [row["id"] for row, _ in result["updates"]]
    _snapshot(session, ids, "pre")
    for row, diff in result["updates"]:
        laptop = session.get(Laptop, row["id"])
        for field, (_, new) in diff.items():
            setattr(laptop, field, new)
        session.add(laptop)
    session.flush()
    _snapshot(session, ids, "post")


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
        rows, texts = _read(session)
        result = plan(rows, texts)
        for row, diff in result["updates"]:
            flag = " ACTIVE" if row["status"] == "active" else ""
            print(f"UPDATE {str(row['id'])[:8]} {row['model_code']} [{row['status']}]{flag}")
            for field, (old, new) in diff.items():
                print(f"  {field}: {str(old)[:90]!r} -> {str(new)[:90]!r}")
        for s in result["skipped"]:
            print(f"SKIP   {s['id'][:8]} {s['model_code']}: {s['reason']}")
        for p in result["parse_failures"]:
            print(f"FAILED {p['id'][:8]} {p['model_code']}: "
                  + "; ".join(f"{f['field']}: {f['reason']}" for f in p["failures"]))
        if args.apply:
            apply(session, result)
            session.commit()
        else:
            session.rollback()
    engine.dispose()

    print(f"{len(result['updates'])} updates, {len(result['skipped'])} skipped, "
          f"{len(result['parse_failures'])} rows with parse failures, "
          f"{'written' if args.apply else 'nothing written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
