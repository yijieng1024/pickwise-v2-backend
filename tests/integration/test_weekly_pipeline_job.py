"""One job record per weekly pipeline run (plan step 11, item 8), with stand-in steps:
no site, no LLM, no mail server."""
import pytest
from sqlmodel import select

from app.common.job_model import BackgroundJob, JobStatus, JobType
from app.pipeline.settings import PipelineSettings
from app.scripts import weekly_pipeline as wp

pytestmark = pytest.mark.integration


@pytest.fixture
def pipeline(session, admin_client, monkeypatch):
    """admin_client points every module's engine at the test's connection, which is
    what JobProgress's own short sessions use."""
    monkeypatch.setattr(wp, "send_summary", lambda *a, **k: {"sent": False, "reason": "stubbed"})
    return lambda steps, scheduled=True: (
        monkeypatch.setattr(wp, "STEPS", steps),
        wp.apply(session.connection(), PipelineSettings(report_email=""), scheduled),
    )[1]


def _jobs(session):
    return session.exec(select(BackgroundJob).where(BackgroundJob.job_type == JobType.WEEKLY_PIPELINE)).all()


def test_a_run_is_one_job_started_by_the_schedule_outside_render(session, pipeline):
    result = pipeline((("crawl", lambda run: {"asus": {}}), ("scrape", lambda run: {"asus": {}})))

    [job] = _jobs(session)
    assert (job.status, job.started_by, job.runs_outside_render) == (JobStatus.COMPLETED, "schedule", True)
    assert (job.processed_count, job.total_count) == (2, 2)
    assert job.result["steps"] == {"crawl": {"asus": {}}, "scrape": {"asus": {}}}
    assert result["job_id"] == str(job.id)


def test_a_manual_run_is_started_by_an_admin(session, pipeline):
    pipeline((("crawl", lambda run: {}),), scheduled=False)
    assert _jobs(session)[0].started_by == "admin"


def test_a_busy_lock_skips_the_run_and_still_leaves_a_job(session, pipeline):
    busy = BackgroundJob(job_type=JobType.BULK_SCRAPE, status=JobStatus.PROCESSING)
    session.add(busy)
    session.commit()
    ran = []

    result = pipeline((("crawl", lambda run: ran.append(1) or {}),))

    assert ran == []
    [job] = _jobs(session)
    assert job.status == JobStatus.COMPLETED
    assert job.result["skipped"] == result["skipped"] == f"another scrape or processing job is active (job {busy.id})"
    assert job.result["email"] == {"sent": False, "reason": "stubbed"}


def test_a_stopped_run_is_a_failed_job_that_keeps_what_ran(session, pipeline):
    def broken(run):
        raise RuntimeError("no database")

    pipeline((("crawl", lambda run: {"asus": {}}), ("scrape", broken), ("process", lambda run: {})))

    [job] = _jobs(session)
    assert job.status == JobStatus.FAILED
    assert job.error_message == "stopped at scrape: RuntimeError: no database"
    assert (job.result["steps"], job.result["not_run"]) == ({"crawl": {"asus": {}}}, ["process"])
    assert job.result["email"] == {"sent": False, "reason": "stubbed"}
