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
        "guide_id": str(guide.id), "filled": 0, "confirmed": 0, "differs_from_guide": 0,
        "awaiting_confirmation": 0, "skipped_unclear": 1,
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


def test_a_finished_apply_counts_matched_laptops_not_guide_rows(admin_client, session, brand):
    _laptop(session, brand)
    guide = _guide(session, (SKU, 32999.0), ("G835L-AAAAA", 2.0), ("G835L-BBBBB", 3.0))

    response = admin_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply")

    session.expire_all()
    job = session.get(BackgroundJob, response.json()["job_id"])
    assert (job.processed_count, job.total_count) == (1, 1)


def test_a_second_apply_while_one_runs_is_refused(admin_client, session, brand):
    guide = _guide(session, (SKU, 32999.0))
    session.add(BackgroundJob(job_type=JobType.PRICE_GUIDE_APPLY, status=JobStatus.PROCESSING, params={}, total_count=1))
    session.commit()

    assert admin_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply").status_code == 409


def _use(admin_client, guide, *laptops):
    return admin_client.post(
        f"/api/v2/admin/price-guides/{guide.id}/use-guide-price", json={"laptop_ids": [str(l.id) for l in laptops]},
    )


def _differing(session, brand):
    """Three page-priced laptops the guide disagrees with."""
    laptops = [
        _laptop(session, brand, sku=f"G835L-{name}", price=30000.0, source=PriceSource.PAGE.value, checked=OLD)
        for name in ("AAAAA", "BBBBB", "CCCCC")
    ]
    return laptops, _guide(session, ("G835L-AAAAA", 31000.0), ("G835L-BBBBB", 32000.0), ("G835L-CCCCC", 33000.0))


def test_use_guide_price_changes_the_selected_laptops_only(admin_client, session, brand):
    (first, second, unselected), guide = _differing(session, brand)

    response = _use(admin_client, guide, first, second)

    assert response.status_code == 200
    assert response.json()["updated"] == 2
    session.expire_all()
    for laptop, price in ((first, 31000.0), (second, 32000.0)):
        assert laptop.price_rm == price
        assert laptop.price_checked_at > OLD
        [_, row] = _history(session, laptop.id)
        assert (row.price_rm, row.source, row.price_guide_id) == (price, PriceSource.GUIDE.value, guide.id)
    assert unselected.price_rm == 30000.0
    assert unselected.price_checked_at == OLD
    assert len(_history(session, unselected.id)) == 1


def test_a_later_guide_updates_a_price_taken_with_use_guide_price(admin_client, session, brand):
    (first, _, _), guide = _differing(session, brand)
    _use(admin_client, guide, first)

    _apply(session, _guide(session, ("G835L-AAAAA", 29999.0), sha="d" * 64))

    session.expire_all()
    assert first.price_rm == 29999.0


def test_a_selection_with_a_laptop_off_the_list_writes_nothing(admin_client, session, brand):
    (first, *_), guide = _differing(session, brand)
    not_in_guide = _laptop(session, brand, sku="G835L-ZZZZZ", price=30000.0, source=PriceSource.PAGE.value)

    response = _use(admin_client, guide, first, not_in_guide)

    assert response.status_code == 409
    assert str(not_in_guide.id) in response.json()["detail"]
    session.expire_all()
    assert first.price_rm == 30000.0
    assert len(_history(session, first.id)) == 1


def test_use_guide_price_waits_for_a_running_apply(admin_client, session, brand):
    (first, *_), guide = _differing(session, brand)
    session.add(BackgroundJob(job_type=JobType.PRICE_GUIDE_APPLY, status=JobStatus.PROCESSING, params={}, total_count=1))
    session.commit()

    assert _use(admin_client, guide, first).status_code == 409


def test_use_guide_price_needs_a_selection(admin_client, session, brand):
    assert _use(admin_client, _guide(session, (SKU, 1.0))).status_code == 422


def test_a_suffix_match_is_listed_and_apply_writes_only_the_exact_match(session, brand):
    twin = _laptop(session, brand, sku="GU606A-RTB128WP", status=LaptopStatus.INACTIVE.value)
    exact = _laptop(session, brand, sku="G835L-AAAAA", status=LaptopStatus.INACTIVE.value)
    guide = _guide(session, ("GU606A-RTB128W", 23999.0), ("G835L-AAAAA", 32999.0))

    body = price_guide_service.preview(session, guide)
    result = _apply(session, guide)

    assert body["counts"]["matched"] == 1
    assert body["counts"]["matched_by_suffix_rule"] == 1
    [entry] = body["matched_by_suffix_rule"]
    assert (entry["id"], entry["catalog_sku"], entry["guide_sku"], entry["guide_price_rm"]) == (
        str(twin.id), "GU606A-RTB128WP", "GU606A-RTB128W", 23999.0,
    )
    assert (result["filled"], result["awaiting_confirmation"]) == (1, 1)
    session.expire_all()
    assert exact.price_rm == 32999.0
    assert twin.price_rm == 0.0
    assert twin.price_checked_at is None
    assert _history(session, twin.id) == []


def _base_code_laptop(session, brand, price=5300.0):
    laptop = make_laptop(
        brand.id, product_name="ASUS Vivobook S16 (M3607)", model_code="asus-vivobook-s16-m3607", price_rm=price,
        processor_model="AMD Ryzen AI 5 330", ram_gb=16, ssd_gb=512, price_checked_at=OLD,
    )
    session.add(laptop)
    session.commit()
    session.add(LaptopPriceHistory(laptop_id=laptop.id, price_rm=price, source=PriceSource.PAGE.value, recorded_at=OLD))
    session.commit()
    return laptop


def _spec_guide(session, sku="M3607K-ASH236WS", price=5199.0):
    guide = _guide(session)
    session.add(PriceGuideRow(
        guide_id=guide.id, sku=sku, model_code=sku.split("-")[0], price_rm=price,
        cpu="AMD Ryzen AI 5 330 Processor", ram_gb=16, ssd_gb=512, page=79,
    ))
    session.commit()
    return guide


def test_a_base_code_match_is_listed_and_apply_writes_nothing(session, brand):
    laptop = _base_code_laptop(session, brand, price=0.0)  # 0 would be filled if this were `matched`
    guide = _spec_guide(session)

    body = price_guide_service.preview(session, guide)
    result = _apply(session, guide)

    assert (body["counts"]["matched"], body["counts"]["matched_by_base_code"]) == (0, 1)
    [entry] = body["matched_by_base_code"]
    assert (entry["id"], entry["guide_sku"], entry["guide_price_rm"]) == (str(laptop.id), "M3607K-ASH236WS", 5199.0)
    assert (result["filled"], result["awaiting_confirmation"]) == (0, 1)
    session.expire_all()
    assert laptop.price_rm == 0.0
    assert laptop.price_checked_at == OLD


def test_use_guide_price_writes_a_base_code_match(admin_client, session, brand):
    laptop = _base_code_laptop(session, brand)
    guide = _spec_guide(session)

    assert _use(admin_client, guide, laptop).status_code == 200

    session.expire_all()
    assert laptop.price_rm == 5199.0
    [_, row] = _history(session, laptop.id)
    assert (row.source, row.price_guide_id) == (PriceSource.GUIDE.value, guide.id)


def _panel_conflict(session, brand, price=0.0, source="none"):
    laptop = _laptop(session, brand, price=price, source=source)
    laptop.display_type = "IPS-level Panel"
    session.add(laptop)
    session.commit()
    guide = _guide(session, (SKU, 3999.0))
    row = session.exec(select(PriceGuideRow).where(PriceGuideRow.guide_id == guide.id)).one()
    row.panel = "14.0-inch, WUXGA (1920 x 1200) OLED 16:10"
    session.add(row)
    session.commit()
    return laptop, guide


def test_a_panel_conflict_is_listed_with_its_reason_and_apply_writes_nothing(session, brand):
    laptop, guide = _panel_conflict(session, brand)

    body = price_guide_service.preview(session, guide)
    result = _apply(session, guide)

    assert (body["counts"]["matched"], body["counts"]["conflict"]) == (0, 1)
    [entry] = body["conflict"]
    assert (entry["id"], entry["reason"], entry["catalog_panel"]) == (str(laptop.id), "panel", "IPS-level Panel")
    assert entry["rows"][0]["panel"] == "14.0-inch, WUXGA (1920 x 1200) OLED 16:10"
    assert result["filled"] == 0
    session.expire_all()
    assert laptop.price_rm == 0.0
    assert _history(session, laptop.id) == []


def test_use_guide_price_refuses_a_panel_conflict(admin_client, session, brand):
    # A page price, so this would sit on the differs list if the panel were not compared.
    laptop, guide = _panel_conflict(session, brand, price=5499.0, source=PriceSource.PAGE.value)

    assert _use(admin_client, guide, laptop).status_code == 409

    session.expire_all()
    assert laptop.price_rm == 5499.0


def test_use_guide_price_writes_a_suffix_match(admin_client, session, brand):
    twin = _laptop(session, brand, sku="GU606A-RTB128WP")
    guide = _guide(session, ("GU606A-RTB128W", 23999.0))

    assert _use(admin_client, guide, twin).status_code == 200

    session.expire_all()
    assert twin.price_rm == 23999.0
    assert twin.price_checked_at is not None
    [row] = _history(session, twin.id)
    assert (row.source, row.price_guide_id) == (PriceSource.GUIDE.value, guide.id)


def test_use_guide_price_on_a_suffix_match_already_right_moves_last_checked_only(admin_client, session, brand):
    twin = _laptop(session, brand, sku="GU606A-RTB128WP", price=23999.0, source=PriceSource.PAGE.value, checked=OLD)
    guide = _guide(session, ("GU606A-RTB128W", 23999.0))

    assert _use(admin_client, guide, twin).status_code == 200

    session.expire_all()
    assert twin.price_checked_at > OLD
    assert [row.source for row in _history(session, twin.id)] == [PriceSource.PAGE.value]


def test_the_preview_reads_only(admin_client, session, brand):
    laptop = _laptop(session, brand)
    guide = _guide(session, (SKU, 32999.0), ("G835L-NOTINCATALOG", 100.0), ("G835L-NOPRICE", None))

    body = admin_client.get(f"/api/v2/admin/price-guides/{guide.id}/preview").json()

    session.refresh(laptop)
    assert laptop.price_rm == 0.0
    assert body["counts"] == {
        "matched": 1, "differs_from_guide": 0, "matched_by_suffix_rule": 0, "matched_by_base_code": 0,
        "ambiguous": 0, "conflict": 0,
        "unmatched_guide": 1, "unpriced": 1, "uncovered": 0,
    }
    assert body["matched"][0]["action"] == "fill"
    assert body["unmatched_guide"][0]["sku"] == "G835L-NOTINCATALOG"


def test_an_unknown_guide_is_404(admin_client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert admin_client.get(f"/api/v2/admin/price-guides/{missing}/preview").status_code == 404
    assert admin_client.post(f"/api/v2/admin/price-guides/{missing}/apply").status_code == 404
    guide = PriceGuide(id=missing, kind="rog", filename="", pdf_sha256="", row_count=0)
    assert _use(admin_client, guide, guide).status_code == 404
    assert _delete(admin_client, guide).status_code == 404


def _delete(client, guide):
    return client.delete(f"/api/v2/admin/price-guides/{guide.id}")


def _rows(session, guide_id):
    return session.exec(select(PriceGuideRow).where(PriceGuideRow.guide_id == guide_id)).all()


def test_delete_removes_the_guide_and_its_rows_only(admin_client, session):
    guide = _guide(session, (SKU, 1.0), ("G835L-AAAAA", 2.0))
    kept = _guide(session, (SKU, 3.0), sha="k" * 64)
    guide_id = guide.id

    assert _delete(admin_client, guide).status_code == 204

    session.expire_all()
    assert session.get(PriceGuide, guide_id) is None
    assert _rows(session, guide_id) == []
    assert session.get(PriceGuide, kept.id) is not None
    assert len(_rows(session, kept.id)) == 1


def test_a_guide_a_price_came_from_cannot_be_deleted(admin_client, session, brand):
    laptop = _laptop(session, brand)
    guide = _guide(session, (SKU, 32999.0))
    _apply(session, guide)

    response = _delete(admin_client, guide)

    assert response.status_code == 409
    assert "1 price came" in response.json()["detail"]
    session.expire_all()
    assert session.get(PriceGuide, guide.id) is not None
    assert len(_rows(session, guide.id)) == 1
    assert _history(session, laptop.id)[0].price_guide_id == guide.id


def test_a_confirm_only_apply_stays_deletable(admin_client, session, brand):
    laptop = _laptop(session, brand, price=32999.0, source=PriceSource.PAGE.value, checked=OLD)
    guide = _guide(session, (SKU, 32999.0))
    guide_id = guide.id
    assert _apply(session, guide)["confirmed"] == 1

    assert _delete(admin_client, guide).status_code == 204

    session.expire_all()
    assert session.get(PriceGuide, guide_id) is None
    assert laptop.price_checked_at > OLD


def test_delete_waits_for_a_running_apply(admin_client, session):
    guide = _guide(session, (SKU, 1.0))
    session.add(BackgroundJob(job_type=JobType.PRICE_GUIDE_APPLY, status=JobStatus.PROCESSING, params={}, total_count=1))
    session.commit()

    assert _delete(admin_client, guide).status_code == 409

    session.expire_all()
    assert session.get(PriceGuide, guide.id) is not None
    assert len(_rows(session, guide.id)) == 1


def test_a_price_written_during_the_delete_is_a_409_and_deletes_nothing(admin_client, session, brand, monkeypatch):
    _laptop(session, brand)
    guide = _guide(session, (SKU, 32999.0))
    guide_id = guide.id
    _apply(session, guide)
    monkeypatch.setattr(price_guide_service, "_history_rows", lambda *_: 0)  # the count ran before the write

    assert _delete(admin_client, guide).status_code == 409

    session.expire_all()
    assert session.get(PriceGuide, guide_id) is not None
    assert len(_rows(session, guide_id)) == 1


def test_the_routes_are_admin_only(api_client, session):
    guide = _guide(session, (SKU, 1.0))

    assert _delete(api_client, guide).status_code == 403
    assert session.get(PriceGuide, guide.id) is not None

    assert api_client.get("/api/v2/admin/price-guides").status_code == 403
    assert api_client.get(f"/api/v2/admin/price-guides/{guide.id}/preview").status_code == 403
    assert api_client.post(f"/api/v2/admin/price-guides/{guide.id}/apply").status_code == 403
    assert _use(api_client, guide, guide).status_code == 403


class _Dispatch:
    def __init__(self, status_code=204, body=None):
        self.status_code = status_code
        self.text = "refused"
        self.body = body
        self.calls = []

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body

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


def test_a_dispatch_that_names_its_run_passes_the_id_on(admin_client, monkeypatch, token):
    monkeypatch.setattr(httpx, "post", _Dispatch(status_code=200, body={"workflow_run_id": 77}))

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": LINK, "kind": "rog"})

    assert response.status_code == 202
    assert response.json()["run_id"] == 77


def test_a_204_dispatch_has_no_run_id(admin_client, monkeypatch, token):
    monkeypatch.setattr(httpx, "post", _Dispatch())

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": LINK, "kind": "rog"})

    assert response.status_code == 202
    assert response.json()["run_id"] is None


RUN_AT = "2026-11-02T12:21:20Z"


def _run(run_id=7, status="completed", conclusion="success"):
    return {"id": run_id, "status": status, "conclusion": conclusion, "html_url": "https://github.test/run", "created_at": RUN_AT}


class _GitHub:
    """Answers the three GET paths the status route uses, and records them."""

    def __init__(self, runs=None, run=None, steps=(), status_code=200):
        self.answers = {"workflows/": {"workflow_runs": runs or []}, "/jobs": {"jobs": [{"steps": list(steps)}]}, "runs/": run}
        self.status_code = status_code
        self.text = "refused"
        self.paths = []

    def __call__(self, url, **kwargs):
        self.paths.append(url.split("/actions/")[1])
        self.current = next(v for k, v in self.answers.items() if k in self.paths[-1])
        return self

    def json(self):
        return self.current


def _status(admin_client, query=""):
    return admin_client.get(f"/api/v2/admin/price-guides/extraction{query}")


def test_a_running_extraction_reports_its_step(admin_client, monkeypatch, token):
    steps = [
        {"name": "Download the PDF", "status": "completed", "conclusion": "success"},
        {"name": "Extract the rows", "status": "in_progress", "conclusion": None},
        {"name": "Write to the staging tables", "status": "pending", "conclusion": None},
    ]
    github = _GitHub(runs=[_run(status="in_progress", conclusion=None)], steps=steps)
    monkeypatch.setattr(httpx, "get", github)

    body = _status(admin_client).json()

    assert (body["run_id"], body["status"], body["step"], body["guide_id"]) == (7, "in_progress", "Extract the rows", None)
    assert github.paths == ["workflows/price-guide.yml/runs?per_page=1", "runs/7/jobs"]


def test_a_failed_run_names_the_step_that_failed(admin_client, monkeypatch, token):
    steps = [{"name": "Download the PDF", "status": "completed", "conclusion": "failure"}]
    monkeypatch.setattr(httpx, "get", _GitHub(run=_run(conclusion="failure"), steps=steps))

    body = _status(admin_client, "?run_id=7").json()

    assert (body["conclusion"], body["step"]) == ("failure", "Download the PDF")


def test_a_finished_rerun_reports_what_it_did_and_is_not_a_new_guide(admin_client, session, monkeypatch, token):
    guide = _guide(session, (SKU, 1.0))
    guide.uploaded_at, guide.last_run_id, guide.last_run_note = OLD, 7, "Already stored: filled 97 blank panels"
    session.add(guide)
    session.commit()
    github = _GitHub(run=_run())
    monkeypatch.setattr(httpx, "get", github)

    body = _status(admin_client, "?run_id=7").json()

    assert (body["guide_id"], body["note"], body["is_new_guide"]) == (
        str(guide.id), "Already stored: filled 97 blank panels", False,
    )
    assert github.paths == ["runs/7"], "a finished run needs no step lookup"


def test_a_finished_run_that_stored_a_guide_is_new(admin_client, session, monkeypatch, token):
    guide = _guide(session, (SKU, 1.0))
    guide.last_run_id, guide.last_run_note = 7, "Stored 1 rows"
    guide.uploaded_at = datetime(2026, 11, 2, 12, 22, tzinfo=timezone.utc)
    guide.filename = "guide.pdf"
    session.add(guide)
    session.commit()
    monkeypatch.setattr(httpx, "get", _GitHub(run=_run()))

    body = _status(admin_client, "?run_id=7").json()

    assert (body["filename"], body["is_new_guide"]) == ("guide.pdf", True)


def test_a_finished_run_from_before_the_columns_has_no_guide(admin_client, monkeypatch, token):
    monkeypatch.setattr(httpx, "get", _GitHub(runs=[_run()]))

    body = _status(admin_client).json()

    assert (body["conclusion"], body["guide_id"], body["note"]) == ("success", None, None)


def test_no_run_yet_is_null(admin_client, monkeypatch, token):
    monkeypatch.setattr(httpx, "get", _GitHub())

    response = _status(admin_client)

    assert (response.status_code, response.json()) == (200, None)


def test_a_status_check_github_refuses_is_a_502_without_the_token(admin_client, monkeypatch, token, caplog):
    monkeypatch.setattr(httpx, "get", _GitHub(status_code=403))

    response = _status(admin_client)

    assert response.status_code == 502
    assert "secret-token" not in response.text
    assert "secret-token" not in caplog.text


def test_github_out_of_reach_is_a_502(admin_client, monkeypatch, token):
    def unreachable(*_, **__):
        raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(httpx, "get", unreachable)

    assert _status(admin_client).status_code == 502


def test_the_status_check_needs_the_token(admin_client, monkeypatch):
    monkeypatch.setattr(price_guide_router.settings, "github_dispatch_token", None)

    response = _status(admin_client)

    assert response.status_code == 400
    assert "GITHUB_DISPATCH_TOKEN" in response.json()["detail"]


def test_no_token_is_a_clear_400(admin_client, monkeypatch):
    monkeypatch.setattr(price_guide_router.settings, "github_dispatch_token", None)

    response = admin_client.post("/api/v2/admin/price-guides/extract", json={"drive_link": LINK, "kind": "rog"})

    assert response.status_code == 400
    assert "GITHUB_DISPATCH_TOKEN" in response.json()["detail"]
