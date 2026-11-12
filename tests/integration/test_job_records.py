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
def start(admin_client, session, brand):
    """Each route that starts a scrape or processing job, by name."""
    target = ScrapeTarget(url="https://example.test/laptop", brand_id=brand.id)
    session.add(target)
    session.commit()
    calls = {
        "bulk-scrape": lambda: admin_client.post(f"{API}/scraper/bulk-scrape", json={"brand_id": str(brand.id)}),
        "scrape-targets": lambda: admin_client.post(
            f"{API}/scraper/scrape-targets", json={"target_ids": [str(target.id)]}
        ),
        "process-pending": lambda: admin_client.post(f"{API}/processor/process-pending"),
        "retry-failed": lambda: admin_client.post(f"{API}/processor/retry-failed"),
    }
    return lambda route: calls[route]()


@pytest.mark.parametrize("route", ["bulk-scrape", "scrape-targets", "process-pending", "retry-failed"])
@pytest.mark.parametrize("running", JobType.SCRAPE_AND_PROCESS)
def test_scrape_and_process_refuse_a_second_start(start, session, route, running):
    job = _active(session, running)

    res = start(route)

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "job_running"
    assert res.json()["detail"]["job_id"] == str(job.id)
    assert len(session.exec(select(BackgroundJob)).all()) == 1  # refused before a job row exists


def test_a_job_outside_render_holds_the_lock_too(start, session):
    _active(session, JobType.BULK_SCRAPE, runs_outside_render=True)
    assert start("process-pending").status_code == 409


@pytest.mark.parametrize("route", ["bulk-scrape", "process-pending", "retry-failed"])
def test_a_finished_or_unrelated_job_does_not_block(start, session, route):
    session.add(BackgroundJob(job_type=JobType.PROCESS_PENDING, status=JobStatus.FAILED))
    session.commit()
    _active(session, JobType.GENERATE_PICK_SCORES)

    assert start(route).status_code == 202
