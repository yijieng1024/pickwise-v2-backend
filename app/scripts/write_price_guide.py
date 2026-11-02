"""
Store one extraction's rows in the staging tables, `price_guides` and
`price_guide_rows`. Nothing else: a price reaches `laptops` or
`laptop_price_history` only through the admin apply.

    python -m app.scripts.write_price_guide rows.json [--uploaded-by USER_ID]

Writes to TEST_DATABASE_URL unless PRICE_GUIDE_TARGET=production is set, and checks
that the URL is the database it claims to be (app/common/db_target.py). Logs the
host only. The PDF's sha256 identifies a guide, so running the same file again adds
nothing: it only fills `panel` on rows stored before that column existed.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.laptops.price_guide_model import PriceGuide, PriceGuideKind, PriceGuideRow

_GUIDES = PriceGuide.__table__
_ROWS = PriceGuideRow.__table__


def write_guide(connection: Connection, payload: dict, uploaded_by: Optional[uuid.UUID] = None) -> Optional[uuid.UUID]:
    """The new guide's id, or None if this PDF is already stored. The caller owns
    the transaction, so a failure part-way stores nothing."""
    rows = payload["rows"]
    if not rows:
        raise ValueError("no rows: an extraction that found nothing is not a guide")
    kind = PriceGuideKind(payload["kind"]).value

    if connection.execute(select(_GUIDES.c.id).where(_GUIDES.c.pdf_sha256 == payload["pdf_sha256"])).first():
        return None

    guide = PriceGuide(
        kind=kind,
        filename=payload["filename"],
        pdf_sha256=payload["pdf_sha256"],
        edition_text=payload.get("edition_text"),
        uploaded_by=uploaded_by,
        row_count=len(rows),
    )
    connection.execute(insert(_GUIDES).values(
        id=guide.id, kind=guide.kind, filename=guide.filename, pdf_sha256=guide.pdf_sha256,
        edition_text=guide.edition_text, uploaded_at=guide.uploaded_at, uploaded_by=uploaded_by,
        row_count=guide.row_count,
    ))
    connection.execute(insert(_ROWS), [
        {
            "id": uuid.uuid4(),
            "guide_id": guide.id,
            "sku": row["sku"],
            "model_code": row["sku"].split("-")[0],
            "cpu": row.get("cpu"),
            "ram_gb": row.get("ram_gb"),
            "ssd_gb": row.get("ssd_gb"),
            "price_rm": row.get("price_rm"),
            "page": row["page"],
            "panel": row.get("panel"),
        }
        for row in rows
    ])
    return guide.id


def fill_panels(connection: Connection, payload: dict) -> int:
    """Set the panel on this PDF's stored rows that have none, and nothing else. A
    guide a price came from cannot be deleted and extracted afresh, so this is how it
    gets its panels. Returns the rows changed."""
    guide_id = connection.execute(select(_GUIDES.c.id).where(_GUIDES.c.pdf_sha256 == payload["pdf_sha256"])).scalar_one()
    return sum(
        connection.execute(
            update(_ROWS)
            .where(_ROWS.c.guide_id == guide_id, _ROWS.c.sku == row["sku"], _ROWS.c.panel.is_(None))
            .values(panel=row["panel"])
        ).rowcount
        for row in payload["rows"] if row.get("panel")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rows", type=Path)
    parser.add_argument("--uploaded-by", default="", help="the admin's user id; empty when started by hand")
    args = parser.parse_args()

    url = write_target("PRICE_GUIDE_TARGET")
    print(f"writing to {host_of(url)}")
    payload = json.loads(args.rows.read_text(encoding="utf-8"))
    uploaded_by = uuid.UUID(args.uploaded_by) if args.uploaded_by else None

    engine = create_engine(url)
    with engine.begin() as connection:
        guide_id = write_guide(connection, payload, uploaded_by)
        filled = fill_panels(connection, payload) if guide_id is None else 0
    engine.dispose()

    if guide_id is None:
        print(f"{payload['filename']} is already stored; filled {filled} blank panels, wrote nothing else")
    else:
        print(f"stored {len(payload['rows'])} rows as guide {guide_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
