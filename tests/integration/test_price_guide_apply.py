"""Preview and apply against real Postgres. A wrong row here is a wrong price on a
public page, so the fill rule and the one-transaction promise are what is tested."""
from datetime import datetime, timezone

import httpx
import pytest
from sqlmodel import select

from app.admin import price_guide_router
from app.common.job_model import BackgroundJob, JobStatus, JobType
from app.laptops import price_guide_service
from app.laptops.laptop_models import Laptop, LaptopPriceHistory, LaptopStatus, PriceSource
from app.laptops.price_guide_model import PriceGuide, PriceGuideRow
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
SKU = "G835L-XGTQ284WP"


def _guide(session, *rows, sha="a" * 64):
    guide = PriceGuide(kind="rog", filename="guide.pdf", pdf_sha256=sha, row_count=len(rows))
    session.add(guide)
    session.commit()
    for sku, price in rows:
        session.add(PriceGuideRow(guide_id=guide.id, sku=sku, model_code=sku.split("-")[0], price_rm=price, page=75))
    session.commit()
    return guide


def _laptop(session, brand, *, sku=SKU, price=0.0, source=None, status=LaptopStatus.ACTIVE.value, checked=None):
    laptop = make_laptop(brand.id, manufacturer_sku=sku, price_rm=price, status=status, price_checked_at=checked)
    session.add(laptop)
    session.commit()
    if price and source != "none":
        session.add(LaptopPriceHistory(laptop_id=laptop.id, price_rm=price, source=source, recorded_at=OLD))
        session.commit()
    return laptop


def _history(session, laptop_id):
    return session.exec(
        select(LaptopPriceHistory).where(LaptopPriceHistory.laptop_id == laptop_id)
        .order_by(LaptopPriceHistory.recorded_at)
    ).all()


def _apply(session, guide):
    return price_guide_service.apply_guide(session, guide.id)


def test_a_zero_price_is_filled_with_a_guide_history_row(session, brand):
    laptop = _laptop(session, brand, status=LaptopStatus.INACTIVE.value)
    guide = _guide(session, (SKU, 32999.0))

    result = _apply(session, guide)

    session.refresh(laptop)
    assert result["filled"] == 1
    assert laptop.price_rm == 32999.0
    assert laptop.price_checked_at is not None
    [row] = _history(session, laptop.id)
    assert (row.price_rm, row.source, row.price_guide_id) == (32999.0, PriceSource.GUIDE.value, guide.id)


def test_a_price_from_a_guide_is_overwritten(session, brand):
    laptop = _laptop(session, brand, price=30000.0, source=PriceSource.GUIDE.value)

    _apply(session, _guide(session, (SKU, 32999.0)))

    session.refresh(laptop)
    assert laptop.price_rm == 32999.0


@pytest.mark.parametrize("source", [PriceSource.PAGE.value, PriceSource.MANUAL.value, None])
def test_a_page_manual_or_unknown_price_is_never_overwritten(session, brand, source):
    laptop = _laptop(session, brand, price=30000.0, source=source, checked=OLD)
    guide = _guide(session, (SKU, 32999.0))

    result = _apply(session, guide)

    session.refresh(laptop)
    assert result["differs_from_guide"] == 1
    assert laptop.price_rm == 30000.0
    assert laptop.price_checked_at == OLD
    assert len(_history(session, laptop.id)) == 1

    [entry] = price_guide_service.preview(session, guide)["differs_from_guide"]
    assert (entry["price_rm"], entry["guide_price_rm"], entry["price_source"]) == (30000.0, 32999.0, source)


def test_an_unchanged_price_writes_no_history_but_moves_last_checked(session, brand):
    laptop = _laptop(session, brand, price=32999.0, source=PriceSource.PAGE.value, checked=OLD)

    result = _apply(session, _guide(session, (SKU, 32999.0)))

    session.refresh(laptop)
    assert result["confirmed"] == 1
    assert laptop.price_checked_at > OLD
    assert len(_history(session, laptop.id)) == 1


def test_applying_twice_writes_nothing_the_second_time(session, brand):
    filled = _laptop(session, brand, sku="G835L-AAAAA")
    confirmed = _laptop(session, brand, sku="G835L-BBBBB", price=5000.0, source=PriceSource.PAGE.value, checked=OLD)
    guide = _guide(session, ("G835L-AAAAA", 32999.0), ("G835L-BBBBB", 5000.0))
    _apply(session, guide)
    session.refresh(filled)
    session.refresh(confirmed)
    before = [(l.price_rm, l.price_checked_at, l.updated_at) for l in (filled, confirmed)]

    result = _apply(session, guide)

    session.refresh(filled)
    session.refresh(confirmed)
    assert (result["filled"], result["confirmed"]) == (0, 0)
    assert [(l.price_rm, l.price_checked_at, l.updated_at) for l in (filled, confirmed)] == before
    assert len(_history(session, filled.id)) == 1


def test_an_inactive_laptop_stays_inactive_and_is_listed(session, brand):
    laptop = _laptop(session, brand, status=LaptopStatus.INACTIVE.value)
    guide = _guide(session, (SKU, 32999.0))

    [listed] = price_guide_service.preview(session, guide)["priced_but_inactive"]
    _apply(session, guide)

    session.refresh(laptop)
    assert listed["id"] == str(laptop.id)
    assert laptop.status == LaptopStatus.INACTIVE.value


def test_a_suspended_laptop_is_left_out(session, brand):
    laptop = _laptop(session, brand, status=LaptopStatus.SUSPENDED.value)
    guide = _guide(session, (SKU, 32999.0))

    assert _apply(session, guide)["filled"] == 0

    session.refresh(laptop)
    assert laptop.price_rm == 0.0


def test_another_brands_laptop_is_never_a_candidate(session, brand):
    from app.laptops.brand_model import LaptopBrand

    other = LaptopBrand(name="Acer", base_scrape_url="https://example.invalid/acer")
    session.add(other)
    session.commit()
    laptop = make_laptop(other.id, manufacturer_sku=SKU, price_rm=0.0)
    session.add(laptop)
    session.commit()

    assert _apply(session, _guide(session, (SKU, 32999.0)))["filled"] == 0


def test_an_unclear_row_writes_nothing(session, brand):
    laptop = _laptop(session, brand, sku=None)
    # Two guide SKUs claim the same catalog row at different prices
    guide = PriceGuide(kind="rog", filename="guide.pdf", pdf_sha256="c" * 64, row_count=2)
    session.add(guide)
    session.commit()
    for sku, price in (("X1403CVA-AAAAA", 4000.0), ("X1403CVA-BBBBB", 4500.0)):
        session.add(PriceGuideRow(
            guide_id=guide.id, sku=sku, model_code="X1403CVA", price_rm=price, page=1,
            cpu=laptop.processor_model, ram_gb=laptop.ram_gb, ssd_gb=laptop.ssd_gb,
        ))
    laptop.product_name = "ASUS ExpertBook X1403CVA"
    session.add(laptop)
    session.commit()

    assert price_guide_service.preview(session, guide)["counts"]["conflict"] == 1
    result = _apply(session, guide)

    session.refresh(laptop)
    assert result == {
        "guide_id": str(guide.id), "filled": 0, "confirmed": 0, "differs_from_guide": 0, "skipped_unclear": 1,
    }
    assert laptop.price_rm == 0.0


def test_a_failed_apply_writes_nothing(admin_client, session, brand, monkeypatch):
    first = _laptop(session, brand, sku="G835L-AAAAA")
    second = _laptop(session, brand, sku="G835L-BBBBB")
    guide = _guide(session, ("G835L-AAAAA", 1000.0), ("G835L-BBBBB", 2000.0))

    # The second laptop fails after the first was already filled
    real = price_guide_service.decide
    calls = []

    def fails_second_time(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("database went away")
        return real(*args)

    monkeypatch.setattr(price_guide_service, "decide", fails_second_time)
    response = admin_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply")

    session.expire_all()
    job = session.get(BackgroundJob, response.json()["job_id"])
    assert job.status == JobStatus.FAILED
    assert "database went away" in job.error_message
    assert session.get(Laptop, first.id).price_rm == 0.0
    assert session.get(Laptop, second.id).price_rm == 0.0
    assert _history(session, first.id) == []


def test_the_apply_route_runs_as_a_job(admin_client, session, brand):
    laptop = _laptop(session, brand)
    guide = _guide(session, (SKU, 32999.0))

    response = admin_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply")

    assert response.status_code == 202
    assert response.json()["job_type"] == JobType.PRICE_GUIDE_APPLY
    session.expire_all()
    job = session.get(BackgroundJob, response.json()["job_id"])
    assert job.status == JobStatus.COMPLETED
    assert job.result["filled"] == 1
    assert session.get(Laptop, laptop.id).price_rm == 32999.0


def test_a_second_apply_while_one_runs_is_refused(admin_client, session, brand):
    guide = _guide(session, (SKU, 32999.0))
    session.add(BackgroundJob(job_type=JobType.PRICE_GUIDE_APPLY, status=JobStatus.PROCESSING, params={}, total_count=1))
    session.commit()

    assert admin_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply").status_code == 409


def test_the_preview_reads_only(admin_client, session, brand):
    laptop = _laptop(session, brand)
    guide = _guide(session, (SKU, 32999.0), ("G835L-NOTINCATALOG", 100.0), ("G835L-NOPRICE", None))

    body = admin_client.get(f"/api/v2/admin/price-guides/{guide.id}/preview").json()

    session.refresh(laptop)
    assert laptop.price_rm == 0.0
    assert body["counts"] == {
        "matched": 1, "differs_from_guide": 0, "ambiguous": 0, "conflict": 0,
        "unmatched_guide": 1, "unpriced": 1, "uncovered": 0,
    }
    assert body["matched"][0]["action"] == "fill"
    assert body["unmatched_guide"][0]["sku"] == "G835L-NOTINCATALOG"


def test_an_unknown_guide_is_404(admin_client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert admin_client.get(f"/api/v2/admin/price-guides/{missing}/preview").status_code == 404
    assert admin_client.post(f"/api/v2/admin/price-guides/{missing}/apply").status_code == 404


def test_the_routes_are_admin_only(api_client, session):
    guide = _guide(session, (SKU, 1.0))

    assert api_client.get("/api/v2/admin/price-guides").status_code == 403
    assert api_client.get(f"/api/v2/admin/price-guides/{guide.id}/preview").status_code == 403
    assert api_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply").status_code == 403


class _Dispatch:
    def __init__(self, status_code=204):
        self.status_code = status_code
        self.text = "refused"
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(price_guide_router.settings, "github_dispatch_token", "secret-token")


LINK = "https://drive.google.com/file/d/1KsbafVQ1vPidb3kgwRXsvNjTVlBcuJJR/view?usp=sharing"


def test_a_pasted_drive_link_starts_the_workflow(admin_client, admin_user, monkeypatch, token):
    dispatch = _Dispatch()
    monkeypatch.setattr(httpx, "post", dispatch)

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": LINK, "kind": "consumer"})

    assert response.status_code == 202
    assert response.json()["drive_file_id"] == "1KsbafVQ1vPidb3kgwRXsvNjTVlBcuJJR"
    [(url, kwargs)] = dispatch.calls
    assert url.endswith("/actions/workflows/price-guide.yml/dispatches")
    assert kwargs["json"]["inputs"] == {
        "drive_file_id": "1KsbafVQ1vPidb3kgwRXsvNjTVlBcuJJR", "kind": "consumer", "uploaded_by": str(admin_user.id),
    }


@pytest.mark.parametrize("link", ["https://example.com/report.pdf", "not a link", "https://drive.google.com/file/d/short/view"])
def test_a_link_that_is_not_a_drive_file_is_refused(admin_client, monkeypatch, token, link):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not call GitHub"))

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": link, "kind": "rog"})

    assert response.status_code == 400


def test_a_refused_dispatch_is_reported_without_the_token(admin_client, monkeypatch, token, caplog):
    monkeypatch.setattr(httpx, "post", _Dispatch(status_code=403))

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": LINK, "kind": "rog"})

    assert response.status_code == 502
    assert "secret-token" not in response.text
    assert "secret-token" not in caplog.text


def test_no_token_is_a_clear_400(admin_client, monkeypatch):
    monkeypatch.setattr(price_guide_router.settings, "github_dispatch_token", None)

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": LINK, "kind": "rog"})

    assert response.status_code == 400
    assert "GITHUB_DISPATCH_TOKEN" in response.json()["detail"]
