"""
Review ingest, retry transcripts, processing (one and bulk) and rematch as
background jobs.

TestClient runs BackgroundTasks after the response, on the test's connection
(see _app_client), so a job has finished by the time `post` returns.
"""
import uuid

import pytest
from sqlmodel import func, select

from app.common.job_model import BackgroundJob, JobStatus, JobType
from tests.integration.test_review_foundation import (  # noqa: F401 - fixtures
    _link,
    _review,
    fake_llm,
    family_laptops,
)

API = "/api/v2/reviews"


def _job(client, body):
    return client.get(f"/api/v2/jobs/{body['job_id']}").json()


def _job_count(session, *types):
    return session.exec(
        select(func.count(BackgroundJob.id)).where(BackgroundJob.job_type.in_(types))
    ).one()


@pytest.fixture
def unpaused(monkeypatch):
    from types import SimpleNamespace

    from app.reviews import router

    monkeypatch.setattr(router, "settings", SimpleNamespace(review_processing_paused=False))


# --- processing --------------------------------------------------------------

def test_process_bulk_returns_202_and_records_the_run(admin_client, session, family_laptops, fake_llm):
    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])

    res = admin_client.post(f"{API}/process-bulk?limit=5")
    assert res.status_code == 202
    assert res.json()["job_type"] == JobType.REVIEW_PROCESS_BULK

    job = _job(admin_client, res.json())
    assert job["status"] == JobStatus.COMPLETED
    assert (job["result"]["done"], job["result"]["failed"], job["result"]["skipped"]) == (1, 0, 0)
    assert job["result"]["chunks_saved"] == 4


def test_a_partial_review_is_a_job_error_with_its_id_and_reason(admin_client, session, family_laptops, fake_llm):
    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])
    fake_llm["fail_at"] = {3}

    job = _job(admin_client, admin_client.post(f"{API}/process/{review.id}").json())

    assert job["status"] == JobStatus.COMPLETED  # per-item failures never fail the job
    assert job["failed_count"] == 1
    assert job["errors"] == [{"item": str(review.id), "error": "1 of 4 windows failed: RuntimeError: 429 quota"}]


def test_paused_processing_is_refused_before_a_job_exists(admin_client, session, family_laptops, monkeypatch):
    from types import SimpleNamespace

    from app.reviews import router

    monkeypatch.setattr(router, "settings", SimpleNamespace(review_processing_paused=True))
    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])

    for url in (f"{API}/process/{review.id}", f"{API}/process-bulk"):
        assert admin_client.post(url).status_code == 409
    assert _job_count(session, JobType.REVIEW_PROCESS, JobType.REVIEW_PROCESS_BULK) == 0


def test_single_process_validates_before_a_job_exists(admin_client, session, family_laptops, unpaused):
    unlinked = _review(session, status="pending")

    assert admin_client.post(f"{API}/process/{uuid.uuid4()}").status_code == 404
    assert admin_client.post(f"{API}/process/{unlinked.id}").status_code == 409
    assert _job_count(session, JobType.REVIEW_PROCESS) == 0


@pytest.mark.parametrize(
    "running, url",
    [
        (JobType.REVIEW_PROCESS, "/process-bulk"),
        (JobType.REVIEW_PROCESS_BULK, "/process-bulk"),
        (JobType.REVIEW_INGEST_BULK, "/retry-transcripts"),
        (JobType.REVIEW_RETRY_TRANSCRIPTS, "/ingest-bulk"),
        (JobType.REVIEW_REMATCH, "/rematch"),
    ],
)
def test_409_with_the_running_job_id(admin_client, session, unpaused, running, url):
    job = BackgroundJob(job_type=running, status=JobStatus.PROCESSING)
    session.add(job)
    session.commit()

    res = admin_client.post(f"{API}{url}")

    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "job_running", "message": res.json()["detail"]["message"],
                                    "job_id": str(job.id)}


def test_a_running_rematch_does_not_block_processing(admin_client, session, family_laptops, fake_llm):
    session.add(BackgroundJob(job_type=JobType.REVIEW_REMATCH, status=JobStatus.PROCESSING))
    session.commit()
    assert admin_client.post(f"{API}/process-bulk").status_code == 202


# --- transcripts, ingest, rematch ------------------------------------------

class _FailingGuard:
    tripped = False
    attempted = 0

    def __init__(self, **_kw):
        pass

    def fetch(self, _video_id):
        from app.reviews.transcript import TranscriptFailure, TranscriptResult

        self.attempted += 1
        return TranscriptResult(segments=None, failure=TranscriptFailure.IP_BLOCKED)


def test_retry_transcripts_records_each_failure_by_review(admin_client, session, monkeypatch):
    from app.reviews import jobs

    review = _review(session, status="rejected", segments=0, failure_reason="network")
    monkeypatch.setattr(jobs, "TranscriptFetchGuard", _FailingGuard)

    job = _job(admin_client, admin_client.post(f"{API}/retry-transcripts?delay_seconds=0").json())

    assert job["status"] == JobStatus.COMPLETED
    assert {"item": str(review.id), "error": "ip_blocked"} in job["errors"]
    assert job["result"]["still_failing"] == {"ip_blocked": job["result"]["failed"]}


def test_ingest_without_active_channels_is_refused_before_a_job_exists(admin_client, session):
    assert admin_client.post(f"{API}/ingest-bulk").status_code == 400
    assert _job_count(session, JobType.REVIEW_INGEST_BULK) == 0


def test_ingest_runs_as_a_job(admin_client, session, family_laptops, monkeypatch):
    from app.reviews import service
    from app.reviews.models import YoutubeChannel

    session.add(YoutubeChannel(channel_id=f"UC{uuid.uuid4().hex[:10]}", channel_name="Chan", active=True))
    session.commit()
    monkeypatch.setattr(service, "discover_videos", lambda *a, **k: [])

    res = admin_client.post(f"{API}/ingest-bulk?limit=1&skip_covered=false")
    assert res.status_code == 202
    job = _job(admin_client, res.json())
    assert job["status"] == JobStatus.COMPLETED
    assert (job["result"]["done"], job["result"]["failed"]) == (1, 0)


def test_rematch_runs_as_a_job(admin_client, session, family_laptops, monkeypatch):
    from app.reviews import jobs

    family, laptops = family_laptops
    _review(session, status="pending")
    monkeypatch.setattr(jobs, "match_laptop", lambda title, s: (laptops[0].id, 90.0))

    job = _job(admin_client, admin_client.post(f"{API}/rematch").json())

    assert job["status"] == JobStatus.COMPLETED
    assert job["result"]["newly_matched"] >= 1 and job["result"]["done"] == job["result"]["newly_matched"]


def test_startup_cleanup_fails_an_interrupted_review_job(admin_client, session):
    """reset_stale_jobs filters on status, not type, so it covers these too."""
    from app.common.job_service import reset_stale_jobs

    job = BackgroundJob(job_type=JobType.REVIEW_PROCESS_BULK, status=JobStatus.PROCESSING)
    session.add(job)
    session.commit()

    reset_stale_jobs()

    session.expire_all()
    assert session.get(BackgroundJob, job.id).status == JobStatus.FAILED
