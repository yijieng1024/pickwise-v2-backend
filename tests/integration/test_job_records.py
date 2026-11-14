"""
Job records (plan step 11, commit 2): who started a job, where its worker runs,
and one lock for scraping and processing.

TestClient runs BackgroundTasks after the response, on the test's connection
(see _app_client), so a job has finished by the time `post` returns.
"""
import pytest
from sqlmodel import select

from app.common.job_model import BackgroundJob, JobStarter, JobStatus, JobType
from app.common.job_service import create_job, reset_stale_jobs
from app.scraper.models import ScrapeTarget

API = "/api/v2"

# What the stand-in crawler finds: two sites, as one ASUS crawl does.
CRAWLED = (
    "https://www.asus.com/my/laptops/a/",
    "https://www.asus.com/my/laptops/b/",
    "https://rog.asus.com/my/laptops/c/",
)


def _active(session, job_type, **fields) -> BackgroundJob:
    job = BackgroundJob(job_type=job_type, status=JobStatus.PROCESSING, **fields)
    session.add(job)
    session.commit()
    return job


# --- the two markers -----------------------------------------------------------

def test_a_job_is_started_by_an_admin_on_render_unless_marked(admin_client, session):
    plain = create_job(session, job_type=JobType.BULK_SCRAPE, total_count=0)
    weekly = create_job(
        session, job_type=JobType.BULK_SCRAPE, total_count=0,
        started_by=JobStarter.SCHEDULE, runs_outside_render=True,
    )

    read = admin_client.get(f"{API}/jobs/{plain.id}").json()
    assert (read["started_by"], read["runs_outside_render"]) == ("admin", False)
    read = admin_client.get(f"{API}/jobs/{weekly.id}").json()
    assert (read["started_by"], read["runs_outside_render"]) == ("schedule", True)


def test_startup_cleanup_leaves_a_job_running_outside_render(admin_client, session):
    """A Render restart did not stop a worker in GitHub Actions, so its job is
    still running. A job in this process was stopped, and is failed as before."""
    outside = _active(session, JobType.BULK_SCRAPE, runs_outside_render=True, started_by=JobStarter.SCHEDULE)
    inside = _active(session, JobType.PROCESS_PENDING)

    reset_stale_jobs()

    session.expire_all()
    assert session.get(BackgroundJob, outside.id).status == JobStatus.PROCESSING
    assert session.get(BackgroundJob, inside.id).status == JobStatus.FAILED


# --- one lock for scraping and processing ----------------------------------------

@pytest.fixture
def start(admin_client, session, brand, monkeypatch):
    """Each route that starts a scrape or processing job, by name. The brand's
    crawler and scraper are stand-ins, so no test here reaches a real site."""
    from app.scraper import bulk_scraper

    async def crawl(start_url, session, brand_id):
        return list(CRAWLED)

    async def scrape(url, brand_id, session):
        return []

    monkeypatch.setitem(bulk_scraper.SCRAPERS, brand.name.lower(), (crawl, scrape))
    target = ScrapeTarget(url="https://example.test/laptop", brand_id=brand.id)
    session.add(target)
    session.commit()
    calls = {
        "feed-crawler": lambda: admin_client.post(f"{API}/scraper/feed-crawler", json={"brand_id": str(brand.id)}),
        "bulk-scrape": lambda: admin_client.post(f"{API}/scraper/bulk-scrape", json={"brand_id": str(brand.id)}),
        "scrape-targets": lambda: admin_client.post(
            f"{API}/scraper/scrape-targets", json={"target_ids": [str(target.id)]}
        ),
        "process-pending": lambda: admin_client.post(f"{API}/processor/process-pending"),
        "retry-failed": lambda: admin_client.post(f"{API}/processor/retry-failed"),
    }
    return lambda route: calls[route]()


@pytest.mark.parametrize(
    "route", ["feed-crawler", "bulk-scrape", "scrape-targets", "process-pending", "retry-failed"]
)
@pytest.mark.parametrize("running", JobType.SCRAPE_AND_PROCESS)
def test_scrape_and_process_refuse_a_second_start(start, session, route, running):
    job = _active(session, running)

    res = start(route)

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "job_running"
    assert res.json()["detail"]["job_id"] == str(job.id)
    assert len(session.exec(select(BackgroundJob)).all()) == 1  # refused before a job row exists
    assert len(session.exec(select(ScrapeTarget)).all()) == 1  # and before a crawl added any target


def test_a_job_outside_render_holds_the_lock_too(start, session):
    _active(session, JobType.BULK_SCRAPE, runs_outside_render=True)
    assert start("process-pending").status_code == 409


@pytest.mark.parametrize("route", ["feed-crawler", "bulk-scrape", "process-pending", "retry-failed"])
def test_a_finished_or_unrelated_job_does_not_block(start, session, route):
    session.add(BackgroundJob(job_type=JobType.PROCESS_PENDING, status=JobStatus.FAILED))
    session.commit()
    _active(session, JobType.GENERATE_PICK_SCORES)

    assert start(route).status_code == 202


# --- a dead job outside Render ----------------------------------------------------

def _hours_ago(hours):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(hours=hours)


def test_an_outside_job_silent_for_7_hours_is_failed_and_stops_blocking(start, session):
    dead = _active(session, JobType.BULK_SCRAPE, runs_outside_render=True, updated_at=_hours_ago(7))

    assert start("process-pending").status_code == 202

    session.expire_all()
    job = session.get(BackgroundJob, dead.id)
    assert job.status == JobStatus.FAILED
    assert job.finished_at is not None and "6 hours" in job.error_message


def test_an_outside_job_updated_an_hour_ago_still_blocks(start, session):
    alive = _active(session, JobType.BULK_SCRAPE, runs_outside_render=True, updated_at=_hours_ago(1))

    assert start("process-pending").status_code == 409

    session.expire_all()
    assert session.get(BackgroundJob, alive.id).status == JobStatus.PROCESSING


def test_startup_cleanup_applies_the_same_6_hour_rule(admin_client, session):
    dead = _active(session, JobType.BULK_SCRAPE, runs_outside_render=True, updated_at=_hours_ago(7))
    alive = _active(session, JobType.BULK_SCRAPE, runs_outside_render=True, updated_at=_hours_ago(1))

    reset_stale_jobs()

    session.expire_all()
    assert session.get(BackgroundJob, dead.id).status == JobStatus.FAILED
    assert session.get(BackgroundJob, alive.id).status == JobStatus.PROCESSING


def test_a_render_job_silent_for_7_hours_still_blocks(start, session):
    """Jobs on Render keep today's behaviour: only a restart fails them."""
    old = _active(session, JobType.BULK_SCRAPE, updated_at=_hours_ago(7))

    assert start("process-pending").status_code == 409

    session.expire_all()
    assert session.get(BackgroundJob, old.id).status == JobStatus.PROCESSING


def test_a_progress_write_stamps_updated_at(admin_client, session):
    from app.common.job_service import JobProgress

    job = _active(session, JobType.BULK_SCRAPE, runs_outside_render=True, updated_at=_hours_ago(7))

    JobProgress(job.id).advance(item="one page")

    session.expire_all()
    assert session.get(BackgroundJob, job.id).updated_at > _hours_ago(1)


# --- the feed crawler as a job ------------------------------------------------------

def test_a_crawl_is_a_job_whose_result_counts_pages_and_new_targets_per_site(start, admin_client, admin_user, session, brand):
    session.add(ScrapeTarget(url=CRAWLED[0], brand_id=brand.id))  # already queued
    session.commit()

    res = start("feed-crawler")

    assert res.status_code == 202
    assert res.json()["job_type"] == JobType.FEED_CRAWLER
    job = admin_client.get(f"{API}/jobs/{res.json()['job_id']}").json()
    assert job["status"] == JobStatus.COMPLETED
    assert (job["started_by"], job["runs_outside_render"]) == ("admin", False)
    assert job["created_by"] == str(admin_user.id)
    assert (job["processed_count"], job["failed_count"]) == (1, 0)
    assert job["result"] == {
        "brand": brand.name,
        "total_found": 3,
        "added_to_queue": 2,
        "sites": {
            "www.asus.com": {"pages_found": 2, "targets_added": 1},
            "rog.asus.com": {"pages_found": 1, "targets_added": 1},
        },
        # The brand's https://example.test/laptop target is on a site this crawl did
        # not cover, so it has not disappeared.
        "missing_pages": [],
    }
    queued = set(session.exec(select(ScrapeTarget.url).where(ScrapeTarget.brand_id == brand.id)).all())
    assert set(CRAWLED) <= queued


def test_a_crawl_that_finds_nothing_is_a_failed_item(start, admin_client, monkeypatch):
    monkeypatch.setattr("tests.integration.test_job_records.CRAWLED", ())

    job = admin_client.get(f"{API}/jobs/{start('feed-crawler').json()['job_id']}").json()

    assert job["status"] == JobStatus.COMPLETED
    assert job["failed_count"] == 1 and job["errors"][0]["error"] == "No URLs found."
    assert job["result"]["total_found"] == 0
