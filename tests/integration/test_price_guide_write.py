"""The workflow's write step against real Postgres: staging tables and nothing else."""
import re

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.laptops.price_guide_model import PriceGuide, PriceGuideRow
from app.scripts.write_price_guide import write_guide
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

PAYLOAD = {
    "kind": "rog",
    "filename": "MY_ROG Product Guide Q3 2026.pdf",
    "pdf_sha256": "b" * 64,
    "edition_text": "ROG 2026",
    "rows": [
        {"sku": "G835L-XGTQ284WP", "price_rm": 32999.0, "cpu": "Intel Core Ultra 9 Processor 290HX Plus",
         "ram_gb": 64, "ssd_gb": 2048, "page": 75},
        {"sku": "GU405A-WSY066W", "price_rm": None, "cpu": None, "ram_gb": None, "ssd_gb": None, "page": 75},
    ],
}


def test_a_guide_and_its_rows_are_stored(session):
    guide_id = write_guide(session.connection(), PAYLOAD)
    session.commit()

    guide = session.get(PriceGuide, guide_id)
    assert (guide.kind, guide.row_count, guide.uploaded_by) == ("rog", 2, None)
    rows = session.exec(select(PriceGuideRow).where(PriceGuideRow.guide_id == guide_id).order_by(PriceGuideRow.sku)).all()
    assert [(r.sku, r.model_code, r.price_rm, r.page) for r in rows] == [
        ("G835L-XGTQ284WP", "G835L", 32999.0, 75),
        ("GU405A-WSY066W", "GU405A", None, 75),
    ]


def test_the_same_pdf_twice_changes_nothing(session):
    assert write_guide(session.connection(), PAYLOAD) is not None

    assert write_guide(session.connection(), PAYLOAD) is None

    assert len(session.exec(select(PriceGuide)).all()) == 1
    assert len(session.exec(select(PriceGuideRow)).all()) == 2


def test_it_touches_only_the_staging_tables(session, brand):
    laptop = make_laptop(brand.id, manufacturer_sku="G835L-XGTQ284WP", price_rm=0.0)
    session.add(laptop)
    session.commit()
    connection = session.connection()
    statements = []

    def record(_conn, _cursor, statement, *_):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        write_guide(connection, PAYLOAD)
    finally:
        event.remove(connection, "before_cursor_execute", record)

    tables = {t for s in statements for t in re.findall(r"(?:FROM|INTO|UPDATE|DELETE FROM)\s+(\w+)", s)}
    assert tables == {"price_guides", "price_guide_rows"}
    session.refresh(laptop)
    assert laptop.price_rm == 0.0
    assert session.exec(select(LaptopPriceHistory)).all() == []


def test_an_extraction_that_found_nothing_is_not_stored(session):
    with pytest.raises(ValueError, match="no rows"):
        write_guide(session.connection(), {**PAYLOAD, "rows": []})

    assert session.exec(select(PriceGuide)).all() == []


def test_an_unknown_kind_is_refused(session):
    with pytest.raises(ValueError):
        write_guide(session.connection(), {**PAYLOAD, "kind": "tablet"})
