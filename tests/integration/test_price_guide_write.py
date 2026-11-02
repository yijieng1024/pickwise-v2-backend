"""The workflow's write step against real Postgres: staging tables and nothing else."""
import re

import pytest
from sqlalchemy import event, update
from sqlmodel import select

from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.laptops.price_guide_model import PriceGuide, PriceGuideRow
from app.scripts.write_price_guide import fill_edition, fill_panels, run_note, store, write_guide
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

PAYLOAD = {
    "kind": "rog",
    "filename": "MY_ROG Product Guide Q3 2026.pdf",
    "pdf_sha256": "b" * 64,
    "edition_text": "ROG 2026",
    "rows": [
        {"sku": "G835L-XGTQ284WP", "price_rm": 32999.0, "cpu": "Intel Core Ultra 9 Processor 290HX Plus",
         "ram_gb": 64, "ssd_gb": 2048, "page": 75, "panel": "18-inch Mini LED"},
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


def _panels(session):
    return {r.sku: r.panel for r in session.exec(select(PriceGuideRow)).all()}


def test_a_stored_row_keeps_its_panel(session):
    write_guide(session.connection(), PAYLOAD)
    session.commit()

    assert _panels(session) == {"G835L-XGTQ284WP": "18-inch Mini LED", "GU405A-WSY066W": None}


def test_a_second_run_fills_only_blank_panels(session):
    write_guide(session.connection(), PAYLOAD)
    session.commit()
    guide_row = session.exec(select(PriceGuideRow).where(PriceGuideRow.sku == "G835L-XGTQ284WP")).one()
    guide_row.panel = "typed by hand"
    session.add(guide_row)
    session.commit()
    again = {**PAYLOAD, "rows": [
        {**PAYLOAD["rows"][0], "price_rm": 1.0},
        {**PAYLOAD["rows"][1], "panel": "16-inch OLED", "price_rm": 2.0},
    ]}

    assert write_guide(session.connection(), again) is None
    assert fill_panels(session.connection(), again) == 1
    session.commit()

    session.expire_all()
    assert _panels(session) == {"G835L-XGTQ284WP": "typed by hand", "GU405A-WSY066W": "16-inch OLED"}
    assert {r.price_rm for r in session.exec(select(PriceGuideRow)).all()} == {32999.0, None}


def test_filling_panels_on_one_guide_leaves_another_alone(session):
    write_guide(session.connection(), {**PAYLOAD, "pdf_sha256": "c" * 64})
    write_guide(session.connection(), {**PAYLOAD, "pdf_sha256": "d" * 64})
    session.commit()
    guides = session.exec(select(PriceGuide)).all()
    session.exec(update(PriceGuideRow).values(panel=None))
    session.commit()

    assert fill_panels(session.connection(), {**PAYLOAD, "pdf_sha256": guides[0].pdf_sha256}) == 1


def test_filling_panels_touches_only_the_staging_tables(session):
    write_guide(session.connection(), PAYLOAD)
    session.commit()
    session.exec(update(PriceGuideRow).values(panel=None))
    session.commit()
    connection = session.connection()
    statements = []

    def record(_conn, _cursor, statement, *_):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        fill_panels(connection, PAYLOAD)
    finally:
        event.remove(connection, "before_cursor_execute", record)

    tables = {t for s in statements for t in re.findall(r"(?:FROM|INTO|UPDATE|DELETE FROM)\s+(\w+)", s)}
    assert tables == {"price_guides", "price_guide_rows"}


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


def _edition(session):
    session.expire_all()
    return session.exec(select(PriceGuide)).one().edition_text


def test_a_second_run_corrects_the_stored_edition(session):
    write_guide(session.connection(), {**PAYLOAD, "edition_text": "ROG Product Guide 2026 3rd Edition 2026 ROG GAMING"})
    session.commit()

    assert fill_edition(session.connection(), {**PAYLOAD, "edition_text": "ROG Product Guide 2026 3rd Edition"}) is True
    assert fill_edition(session.connection(), {**PAYLOAD, "edition_text": "ROG Product Guide 2026 3rd Edition"}) is False
    session.commit()

    assert _edition(session) == "ROG Product Guide 2026 3rd Edition"


def test_a_run_that_read_no_edition_keeps_the_stored_one(session):
    write_guide(session.connection(), PAYLOAD)
    session.commit()

    assert fill_edition(session.connection(), {**PAYLOAD, "edition_text": None}) is False
    session.commit()

    assert _edition(session) == "ROG 2026"


def test_an_edition_on_one_guide_leaves_another_alone(session):
    write_guide(session.connection(), {**PAYLOAD, "pdf_sha256": "e" * 64})
    write_guide(session.connection(), {**PAYLOAD, "pdf_sha256": "f" * 64})
    session.commit()

    fill_edition(session.connection(), {**PAYLOAD, "pdf_sha256": "e" * 64, "edition_text": "new"})
    session.commit()

    session.expire_all()
    assert {g.pdf_sha256[0]: g.edition_text for g in session.exec(select(PriceGuide)).all()} == {"e": "new", "f": "ROG 2026"}


def test_a_new_guide_records_its_run(session):
    assert store(session.connection(), PAYLOAD, run_id=41444009428) == "Stored 2 rows"
    session.commit()

    guide = session.exec(select(PriceGuide)).one()
    assert (guide.last_run_id, guide.last_run_note) == (41444009428, "Stored 2 rows")


def test_a_rerun_records_what_it_filled_on_the_stored_guide_only(session):
    store(session.connection(), PAYLOAD, run_id=1)
    store(session.connection(), {**PAYLOAD, "pdf_sha256": "9" * 64}, run_id=2)
    session.commit()
    session.exec(update(PriceGuideRow).values(panel=None))
    session.commit()
    edited = {**PAYLOAD, "edition_text": "ROG Product Guide 2026 3rd Edition"}

    assert store(session.connection(), edited, run_id=3) == (
        "Already stored: filled 1 blank panels and corrected the edition"
    )
    session.commit()

    session.expire_all()
    guides = {g.pdf_sha256[0]: (g.last_run_id, g.edition_text) for g in session.exec(select(PriceGuide)).all()}
    assert guides == {"b": (3, "ROG Product Guide 2026 3rd Edition"), "9": (2, "ROG 2026")}


def test_a_rerun_with_nothing_to_do_says_so(session):
    store(session.connection(), PAYLOAD, run_id=1)
    assert store(session.connection(), PAYLOAD, run_id=2) == "Already stored: nothing changed"


@pytest.mark.parametrize("args, note", [
    ((97, 0, False), "Stored 97 rows"),
    ((None, 97, False), "Already stored: filled 97 blank panels"),
    ((None, 0, True), "Already stored: corrected the edition"),
    ((None, 0, False), "Already stored: nothing changed"),
])
def test_run_note(args, note):
    assert run_note(*args) == note


def test_an_extraction_that_found_nothing_is_not_stored(session):
    with pytest.raises(ValueError, match="no rows"):
        write_guide(session.connection(), {**PAYLOAD, "rows": []})

    assert session.exec(select(PriceGuide)).all() == []


def test_an_unknown_kind_is_refused(session):
    with pytest.raises(ValueError):
        write_guide(session.connection(), {**PAYLOAD, "kind": "tablet"})
