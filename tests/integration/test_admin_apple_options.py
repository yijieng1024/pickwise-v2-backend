"""
Admin Apple options view and fetch-now (app/admin/apple_options_router.py, 10c step h),
against the test database. Apple is never called: the options step is replaced.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.admin import apple_options_router as admin_options
from app.common.job_model import BackgroundJob, JobStatus, JobType
from app.laptops.brand_model import LaptopBrand
from app.laptops.option_model import LaptopOption
from app.scraper.models import ScrapeTarget
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

KITS = Path(__file__).parents[1] / "unit" / "fixtures" / "apple_kits_pro_2026-11-04.json"
SUMMARY = {"chip_kits": 5, "apple_calls": 12, "stopped": None, "options_failed": [],
           "chip_not_offered": [{"product": "MacBook Pro", "size_inch": 14, "line": "liquid-retina", "chips": ["m9"]}]}


@pytest.fixture
def apple(session):
    brand = LaptopBrand(name="Apple", base_scrape_url="https://www.apple.com/my/mac/")
    session.add(brand)
    session.flush()
    for row in json.loads(KITS.read_text(encoding="utf-8")):
        if row["size_inch"] == 14 and row["line"] == "liquid-retina":
            session.add(LaptopOption(**row))
    m6 = make_laptop(brand.id, model_code="apple-macbook-pro-14-liquid-retina-m6-12c-12g-16gb-1024gb",
                     product_name="MacBook Pro 14 (M6)", price_rm=8299.0, processor_model="Apple M6 (12-core)",
                     gpu_model="12-core GPU", cpu_cores=12, gpu_cores=12, ram_gb=16, ssd_gb=1024,
                     display_size_inch=14.2, part_numbers=["MHWT4ZP/A"])
    session.add(m6)
    session.commit()
    return brand, m6


def _job(session, job_type, status, **kw):
    job = BackgroundJob(job_type=job_type, status=status, total_count=1, **kw)
    session.add(job)
    session.commit()
    return job


def test_the_view_lists_each_standard_config_s_kits_and_the_latest_run_s_problems(admin_client, session, apple):
    _, m6 = apple
    _job(session, JobType.BULK_SCRAPE, JobStatus.COMPLETED, params={"brand_name": "Apple"},
         result={"apple_options": SUMMARY}, finished_at=datetime(2026, 11, 5, tzinfo=timezone.utc))
    body = admin_client.get("/api/v2/admin/apple-options").json()
    [config] = body["configs"]
    assert config["laptop_id"] == str(m6.id) and config["standard_chip"] == "m6-12-12"
    assert {k["chip"] for k in config["kits"]} == {"m6-12-12", "m5pro-15-16", "m5pro-18-20", "m5max-18-32", "m5max-18-40"}
    assert config["fetched_at"]
    assert body["latest_run"]["chip_not_offered"] == SUMMARY["chip_not_offered"]
    assert body["latest_run"]["job_type"] == JobType.BULK_SCRAPE and body["running_job_id"] is None


def test_fetch_now_runs_the_options_step_only_as_a_job(admin_client, session, apple, monkeypatch):
    calls = []

    async def step(work_session):
        calls.append(1)
        return SUMMARY

    monkeypatch.setattr(admin_options, "run_options_step", step)
    resp = admin_client.post("/api/v2/admin/apple-options/fetch")
    assert resp.status_code == 202 and resp.json()["job_type"] == JobType.APPLE_OPTIONS_FETCH
    session.expire_all()
    job = session.get(BackgroundJob, resp.json()["job_id"])
    assert job.status == JobStatus.COMPLETED and job.result == {"apple_options": SUMMARY} and calls == [1]
    assert admin_client.get("/api/v2/admin/apple-options").json()["latest_run"]["job_id"] == str(job.id)


def test_an_edu_failure_is_a_failed_item_of_the_fetch_job_and_named_in_the_view(admin_client, session, apple, monkeypatch):
    problem = "Apple stopped the education-store fetch: HTTP 403. Stored prices were kept"

    async def step(work_session):
        return {**SUMMARY, "edu": {"prices": 0, "stopped": {"reason": "HTTP 403"}, "problem": problem}}

    monkeypatch.setattr(admin_options, "run_options_step", step)
    resp = admin_client.post("/api/v2/admin/apple-options/fetch")
    session.expire_all()
    job = session.get(BackgroundJob, resp.json()["job_id"])
    assert (job.total_count, job.succeeded_count, job.failed_count) == (2, 1, 1)
    assert any(problem in str(e) for e in job.errors)
    assert admin_client.get("/api/v2/admin/apple-options").json()["latest_run"]["edu"]["problem"] == problem


def test_fetch_now_refuses_while_an_apple_scrape_runs(admin_client, session, apple):
    brand, _ = apple
    running = _job(session, JobType.BULK_SCRAPE, JobStatus.PROCESSING, params={"brand_name": "Apple"})
    resp = admin_client.post("/api/v2/admin/apple-options/fetch")
    assert resp.status_code == 409 and resp.json()["detail"]["job_id"] == str(running.id)

    running.status = JobStatus.COMPLETED
    target = ScrapeTarget(brand_id=brand.id, url="https://www.apple.com/my/shop/buy-mac/macbook-pro")
    session.add_all([running, target])
    session.commit()
    picked = _job(session, JobType.SCRAPE_TARGETS, JobStatus.QUEUED, params={"target_ids": [str(target.id)]})
    assert admin_client.post("/api/v2/admin/apple-options/fetch").json()["detail"]["job_id"] == str(picked.id)


def test_a_non_apple_scrape_does_not_block_and_an_apple_scrape_waits_for_a_fetch(admin_client, session, apple, brand):
    _job(session, JobType.BULK_SCRAPE, JobStatus.PROCESSING, params={"brand_name": "Asus"})
    assert admin_options.active_apple_job(session) is None

    apple_brand, _ = apple
    fetch = _job(session, JobType.APPLE_OPTIONS_FETCH, JobStatus.PROCESSING)
    resp = admin_client.post("/api/v2/scraper/bulk-scrape", json={"brand_id": str(apple_brand.id)})
    assert resp.status_code == 409 and resp.json()["detail"]["job_id"] == str(fetch.id)
    target = ScrapeTarget(brand_id=apple_brand.id, url="https://www.apple.com/my/shop/buy-mac/macbook-air")
    session.add(target)
    session.commit()
    resp = admin_client.post("/api/v2/scraper/scrape-targets", json={"target_ids": [str(target.id)]})
    assert resp.status_code == 409
