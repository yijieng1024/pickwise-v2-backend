"""A PassMark refresh as a background job, against real Postgres.

TestClient runs BackgroundTasks after the response, inside the same call and on
the test's connection (see _app_client), so the job has finished by the time
`post` returns and its row can be read straight back.
"""
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import select

from app.benchmark import passmark_scraper
from app.benchmark.model import BenchmarkRefresh, CPUBenchmark, GPUBenchmark
from app.common import job_service
from app.common.job_model import BackgroundJob, JobStatus, JobType

from .conftest import make_laptop

FIXTURES = Path(__file__).parents[1] / "unit" / "fixtures" / "scraper"
CPU_URL = "/api/v2/benchmarks/scrape/cpu"
GPU_URL = "/api/v2/benchmarks/scrape/gpu"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def no_floor(monkeypatch):
    """The fixtures hold 300 rows, so a real floor would refuse them."""
    sources = {name: replace(s, min_rows=1) for name, s in passmark_scraper.SOURCES.items()}
    monkeypatch.setattr(passmark_scraper, "SOURCES", sources)


def _refreshes(session, source):
    return session.exec(select(BenchmarkRefresh).where(BenchmarkRefresh.source == source)).all()


def _job(session, response) -> BackgroundJob:
    session.expire_all()
    return session.get(BackgroundJob, response.json()["job_id"])


def _running_job(session, job_type=JobType.PASSMARK_CPU) -> BackgroundJob:
    job = BackgroundJob(job_type=job_type, status=JobStatus.PROCESSING, params={}, total_count=1)
    session.add(job)
    session.commit()
    return job


def test_a_run_is_a_job_with_progress_and_the_rows_written(admin_client, session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))

    response = admin_client.post(CPU_URL)

    assert response.status_code == 202
    assert response.json()["job_type"] == JobType.PASSMARK_CPU
    job = _job(session, response)
    assert job.status == JobStatus.COMPLETED
    assert (job.total_count, job.processed_count, job.succeeded_count) == (300, 300, 300)
    assert job.result == {
        "source": "cpu",
        "written": True,
        "rows_written": 300,
        "source_updated_text": "Last updated on the 1st of November 2026",
    }
    assert job.created_by is not None


def test_a_run_upserts_and_writes_exactly_one_refresh_row(admin_client, session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))

    admin_client.post(CPU_URL)

    assert len(session.exec(select(CPUBenchmark)).all()) == 300
    [row] = _refreshes(session, "cpu")
    assert row.row_count == 300
    assert row.source_updated_text == "Last updated on the 1st of November 2026"
    assert _refreshes(session, "gpu") == []


def test_a_gpu_run_writes_the_gpu_table_and_its_own_row(admin_client, session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_gpu.html"))

    response = admin_client.post(GPU_URL)

    assert response.json()["job_type"] == JobType.PASSMARK_GPU
    assert len(session.exec(select(GPUBenchmark)).all()) == 300
    assert [r.row_count for r in _refreshes(session, "gpu")] == [300]


def test_a_second_run_updates_marks_and_adds_a_second_refresh_row(admin_client, session, monkeypatch, no_floor):
    table = '<table id="cputable"><tbody><tr><td>Chip X</td><td>{}</td></tr></tbody></table>'
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: table.format("100"))
    admin_client.post(CPU_URL)
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: table.format("250"))
    admin_client.post(CPU_URL)

    [chip] = session.exec(select(CPUBenchmark).where(CPUBenchmark.cpu_name == "Chip X")).all()
    session.refresh(chip)
    assert chip.cpu_mark == 250
    assert len(_refreshes(session, "cpu")) == 2


def test_a_list_below_the_floor_fails_the_job_with_the_reason(admin_client, session, monkeypatch):
    # The real floor, and a page below it
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))

    job = _job(session, admin_client.post(CPU_URL))

    assert job.status == JobStatus.FAILED
    assert "has 300 rows, below the floor of 5000" in job.error_message
    assert job.result is None
    assert session.exec(select(CPUBenchmark)).all() == []
    assert _refreshes(session, "cpu") == []


def test_a_challenge_page_fails_the_job(admin_client, session, monkeypatch):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: "<html>Just a moment...</html>")

    job = _job(session, admin_client.post(GPU_URL))

    assert job.status == JobStatus.FAILED
    assert "has 0 rows" in job.error_message


@pytest.mark.parametrize("running, url", [
    (JobType.PASSMARK_CPU, CPU_URL),
    (JobType.PASSMARK_CPU, GPU_URL),  # one lock for both lists
    (JobType.PASSMARK_GPU, CPU_URL),
])
def test_a_second_start_while_one_runs_is_refused(admin_client, session, monkeypatch, running, url):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: pytest.fail("a refused run must not fetch"))
    job = _running_job(session, running)
    before = len(session.exec(select(BackgroundJob)).all())

    response = admin_client.post(url)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "job_running",
        "message": "A PassMark refresh is already running.",
        "job_id": str(job.id),
    }
    assert len(session.exec(select(BackgroundJob)).all()) == before


def test_a_finished_run_does_not_block_the_next(admin_client, session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))
    assert admin_client.post(CPU_URL).status_code == 202
    assert admin_client.post(CPU_URL).status_code == 202


def test_a_restart_fails_the_job_as_interrupted(admin_client, session):
    job = _running_job(session)

    assert job_service.reset_stale_jobs() >= 1

    session.expire_all()
    job = session.get(BackgroundJob, job.id)
    assert job.status == JobStatus.FAILED
    assert job.error_message.startswith("Interrupted")


class _StopAfterFirstBatch:
    """Stands in for JobProgress: asks to stop once rows have been reported."""

    def __init__(self):
        self.advanced = 0

    def set_total(self, total):
        pass

    def advance(self, *, count=1):
        self.advanced += count

    @property
    def cancel_requested(self):
        return self.advanced > 0


def test_a_cancelled_run_writes_nothing(session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))

    result = passmark_scraper.refresh("cpu", session, _StopAfterFirstBatch())

    assert result == {"source": "cpu", "written": False, "rows_written": 0}
    assert session.exec(select(CPUBenchmark)).all() == []
    assert _refreshes(session, "cpu") == []


def test_a_run_that_fails_while_writing_leaves_no_marks_and_no_refresh_row(session, monkeypatch, no_floor):
    # The row and the upsert share a transaction: no marks, no date
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))
    monkeypatch.setattr(passmark_scraper, "BenchmarkRefresh", lambda **kw: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        passmark_scraper.refresh("cpu", session, _StopAfterFirstBatch.__new__(_NeverStops))

    session.rollback()
    assert session.exec(select(CPUBenchmark)).all() == []


class _NeverStops(_StopAfterFirstBatch):
    def __init__(self):
        self.advanced = 0

    advanced = 0

    @property
    def cancel_requested(self):
        return False


def test_the_dashboard_shows_the_latest_refresh(admin_client, session):
    def refreshed_at():
        return admin_client.get("/api/v2/admin/dashboard/summary").json()["freshness"]["passmark_refreshed_at"]

    assert refreshed_at() is None

    older = BenchmarkRefresh(source="cpu", row_count=1, refreshed_at=datetime(2026, 8, 1))
    newer = BenchmarkRefresh(source="gpu", row_count=1, refreshed_at=datetime(2026, 11, 1))
    session.add_all([older, newer])
    session.commit()

    assert refreshed_at().startswith("2026-11-01")


def test_the_pick_score_response_carries_each_lists_date(admin_client, session, brand):
    laptop = make_laptop(brand.id)
    session.add(laptop)
    session.add(BenchmarkRefresh(source="cpu", row_count=1, refreshed_at=datetime(2026, 8, 1)))
    session.commit()

    body = admin_client.get(f"/api/v2/laptops/{laptop.id}/pick-scores").json()

    assert body["benchmarks_refreshed_at"]["cpu"].startswith("2026-08-01")
    assert body["benchmarks_refreshed_at"]["gpu"] is None
