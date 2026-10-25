"""
POST /laptops/pick-scores/generate-all as a background job.

TestClient runs BackgroundTasks after the response, inside the same call and
on the test's connection (see _app_client), so the job has finished by the
time `post` returns and its row can be read straight back.
"""

import pytest
from sqlmodel import select

from app.common.job_model import BackgroundJob, JobStatus, JobType
from app.laptops.laptop_models import LaptopPickScore
from tests.integration.conftest import make_laptop

URL = "/api/v2/laptops/pick-scores/generate-all"


@pytest.fixture
def two_laptops(session, brand):
    rows = [
        make_laptop(brand.id, model_code="job-a", product_name="Scored"),
        make_laptop(brand.id, model_code="job-b", product_name="Withheld",
                    processor_model="Unknown", gpu_model="Unknown"),
    ]
    session.add_all(rows)
    session.commit()
    return rows


def _scores(session, laptop_id):
    return {r.use_case: (r.score, r.updated_at)
            for r in session.exec(select(LaptopPickScore).where(LaptopPickScore.laptop_id == laptop_id))}


def test_returns_202_and_records_the_run(admin_client, session, two_laptops):
    res = admin_client.post(URL)
    assert res.status_code == 202
    body = res.json()
    assert body["job_type"] == JobType.GENERATE_PICK_SCORES

    job = admin_client.get(f"/api/v2/jobs/{body['job_id']}").json()
    assert job["status"] == JobStatus.COMPLETED
    assert job["processed_count"] == job["total_count"]
    result = job["result"]
    assert result["written"] is True
    assert result["laptops_withheld"] >= 1
    assert result["laptops_scored"] + result["laptops_withheld"] == result["laptops"]
    assert result["rows_created"] + result["rows_updated"] == result["laptops"] * len(result["use_cases"])
    assert len(_scores(session, two_laptops[0].id)) == 5


def test_409_while_one_is_running(admin_client, session, two_laptops):
    running = BackgroundJob(job_type=JobType.GENERATE_PICK_SCORES, status=JobStatus.PROCESSING)
    session.add(running)
    session.commit()

    res = admin_client.post(URL)
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "job_running"
    assert detail["job_id"] == str(running.id)


def test_a_finished_run_does_not_block_the_next(admin_client, session, two_laptops):
    session.add(BackgroundJob(job_type=JobType.GENERATE_PICK_SCORES, status=JobStatus.FAILED))
    session.commit()
    assert admin_client.post(URL).status_code == 202


def test_startup_cleanup_covers_this_job_type():
    """reset_stale_jobs fails every status in ACTIVE regardless of job type,
    so an interrupted generate-all cannot stay 'processing' or keep 409ing."""
    assert JobStatus.PROCESSING in JobStatus.ACTIVE
    assert JobType.GENERATE_PICK_SCORES in JobType.ALL


class _StopAfterFirst:
    """Stands in for JobProgress: asks to stop once one laptop is done."""

    def __init__(self):
        self.done = 0

    def set_total(self, total):
        pass

    def advance(self, **_):
        self.done += 1

    @property
    def cancel_requested(self):
        return self.done >= 1


def test_a_stopped_run_writes_nothing(session, two_laptops):
    from app.laptops.pickscore_general import generate_all_pick_scores

    generate_all_pick_scores(session)
    before = _scores(session, two_laptops[0].id)

    result = generate_all_pick_scores(session, _StopAfterFirst())
    assert result["written"] is False
    assert _scores(session, two_laptops[0].id) == before


def test_a_failure_before_the_write_leaves_the_old_scores(session, two_laptops, monkeypatch):
    """All-or-nothing: every score is computed before any row is touched."""
    import app.laptops.pickscore_general as general
    from app.laptops.pickscore_general import generate_all_pick_scores

    generate_all_pick_scores(session)
    before = {l.id: _scores(session, l.id) for l in two_laptops}

    real, calls = general.calculate_pick_score, []

    def fail_on_the_second_laptop(product, *args, **kwargs):
        calls.append(product)
        if len(calls) > len(general.USE_CASE_PRIORITIES):
            raise RuntimeError("boom")
        return real(product, *args, **kwargs)

    monkeypatch.setattr(general, "calculate_pick_score", fail_on_the_second_laptop)
    with pytest.raises(RuntimeError):
        generate_all_pick_scores(session)
    session.rollback()
    assert {l.id: _scores(session, l.id) for l in two_laptops} == before
