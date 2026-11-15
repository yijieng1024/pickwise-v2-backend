"""The guide "reviewed" mark (Jack, 2026-11-15): a reviewed row leaves the waiting and
differs lists, and the apply's counts the weekly email reads, until the guide price for
that laptop and SKU changes. _match and what "Use guide price" can select are unchanged."""
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.laptops import price_guide_service
from app.laptops.laptop_models import Laptop, LaptopPriceHistory, LaptopStatus, PriceGuideReview, PriceSource
from app.laptops.price_guide_model import PriceGuide, PriceGuideRow
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _guide(session, *rows, sha="a" * 64):
    guide = PriceGuide(kind="rog", filename="guide.pdf", pdf_sha256=sha, row_count=len(rows))
    session.add(guide)
    session.commit()
    for sku, price in rows:
        session.add(PriceGuideRow(guide_id=guide.id, sku=sku, model_code=sku.split("-")[0], price_rm=price, page=74))
    session.commit()
    return guide


def _laptop(session, brand, sku, price, source=PriceSource.PAGE.value):
    laptop = make_laptop(brand.id, manufacturer_sku=sku, price_rm=price, status=LaptopStatus.ACTIVE.value)
    session.add(laptop)
    session.commit()
    session.add(LaptopPriceHistory(laptop_id=laptop.id, price_rm=price, source=source, recorded_at=OLD))
    session.commit()
    return laptop


def _call(client, guide, path, *laptops, method="POST"):
    return client.request(method, f"/api/v2/admin/price-guides/{guide.id}/{path}",
                          json={"laptop_ids": [str(l.id) for l in laptops]})


@pytest.fixture
def gu605(session, brand):
    """GU605C: the catalog holds -WP at RM 11,299, the guide prints -W at RM 14,999."""
    laptop = _laptop(session, brand, "GU605C-PQR045WP", 11299.0)
    return laptop, _guide(session, ("GU605C-PQR045W", 14999.0))


def test_mark_reviewed_takes_a_row_off_the_list_and_the_email_count(admin_client, session, gu605):
    laptop, guide = gu605
    assert price_guide_service.preview(session, guide)["counts"]["matched_by_suffix_rule"] == 1

    assert _call(admin_client, guide, "review", laptop).status_code == 200

    session.expire_all()
    body = price_guide_service.preview(session, guide)
    assert body["counts"]["matched_by_suffix_rule"] == 0 and body["matched_by_suffix_rule"] == []
    [reviewed] = body["reviewed"]
    assert (reviewed["id"], reviewed["listed_on"], reviewed["decision"]) == (
        str(laptop.id), "matched_by_suffix_rule", "kept")
    assert price_guide_service.apply_guide(session, guide.id)["awaiting_confirmation"] == 0
    session.refresh(laptop)
    assert laptop.price_rm == 11299.0, "marking reviewed keeps the stored price"


def test_a_new_guide_at_the_same_price_stays_reviewed_and_a_new_price_brings_it_back(admin_client, session, gu605):
    laptop, guide = gu605
    _call(admin_client, guide, "review", laptop)
    same = _guide(session, ("GU605C-PQR045W", 14999.0), sha="b" * 64)
    cheaper = _guide(session, ("GU605C-PQR045W", 13999.0), sha="c" * 64)
    session.expire_all()
    assert price_guide_service.preview(session, same)["counts"]["matched_by_suffix_rule"] == 0
    assert price_guide_service.preview(session, cheaper)["counts"]["matched_by_suffix_rule"] == 1


def test_a_reviewed_row_can_still_take_the_guide_price(admin_client, session, gu605):
    laptop, guide = gu605
    _call(admin_client, guide, "review", laptop)
    assert _call(admin_client, guide, "use-guide-price", laptop).status_code == 200
    session.expire_all()
    [review] = session.exec(select(PriceGuideReview)).all()
    assert review.decision == "used_guide" and review.guide_sku == "GU605C-PQR045W"
    session.refresh(laptop)
    assert laptop.price_rm == 14999.0


def test_use_guide_price_marks_the_row_reviewed(admin_client, session, brand):
    laptop = _laptop(session, brand, "G835L-AAAAA", 30000.0)
    guide = _guide(session, ("G835L-AAAAA", 31000.0))
    assert price_guide_service.preview(session, guide)["counts"]["differs_from_guide"] == 1
    _call(admin_client, guide, "use-guide-price", laptop)
    session.expire_all()
    body = price_guide_service.preview(session, guide)
    assert body["counts"]["differs_from_guide"] == 0
    assert session.exec(select(PriceGuideReview)).one().decision == "used_guide"


def test_a_reviewed_differs_row_leaves_the_differs_count(admin_client, session, brand):
    laptop = _laptop(session, brand, "G835L-BBBBB", 30000.0)
    guide = _guide(session, ("G835L-BBBBB", 32000.0))
    _call(admin_client, guide, "review", laptop)
    session.expire_all()
    assert price_guide_service.preview(session, guide)["counts"]["differs_from_guide"] == 0
    assert price_guide_service.apply_guide(session, guide.id)["differs_from_guide"] == 0


def test_undo_puts_it_back(admin_client, session, gu605):
    laptop, guide = gu605
    _call(admin_client, guide, "review", laptop)
    assert _call(admin_client, guide, "review", laptop, method="DELETE").json()["removed"] == 1
    session.expire_all()
    assert price_guide_service.preview(session, guide)["counts"]["matched_by_suffix_rule"] == 1


def test_marking_a_laptop_off_every_list_writes_nothing(admin_client, session, gu605, brand):
    laptop, guide = gu605
    stranger = _laptop(session, brand, "ZZ999-NOPE", 1000.0)
    assert _call(admin_client, guide, "review", laptop, stranger).status_code == 409
    assert session.exec(select(PriceGuideReview)).all() == []


def test_deleting_a_guide_removes_its_reviews(admin_client, session, gu605):
    laptop, guide = gu605
    _call(admin_client, guide, "review", laptop)
    session.expire_all()
    price_guide_service.delete_guide(session, session.get(PriceGuide, guide.id))
    assert session.exec(select(PriceGuideReview)).all() == []


def test_deleting_a_laptop_removes_its_reviews(admin_client, session, gu605):
    laptop, guide = gu605
    _call(admin_client, guide, "review", laptop)
    session.expire_all()
    session.delete(session.get(Laptop, laptop.id))
    session.commit()
    assert session.exec(select(PriceGuideReview)).all() == []
