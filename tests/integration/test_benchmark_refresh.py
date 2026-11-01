"""A PassMark run against real Postgres: the upsert and its refresh row share a
transaction, and what they record reaches the Dashboard and the Pick Score."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import select

from app.benchmark import passmark_scraper
from app.benchmark.model import BenchmarkRefresh, CPUBenchmark, GPUBenchmark

from .conftest import make_laptop

FIXTURES = Path(__file__).parents[1] / "unit" / "fixtures" / "scraper"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def no_floor(monkeypatch):
    """The fixtures hold 300 rows, so a real floor would refuse them."""
    sources = {name: replace(s, min_rows=1) for name, s in passmark_scraper.SOURCES.items()}
    monkeypatch.setattr(passmark_scraper, "SOURCES", sources)


def _refreshes(session, source):
    return session.exec(select(BenchmarkRefresh).where(BenchmarkRefresh.source == source)).all()


def test_a_successful_run_upserts_and_writes_exactly_one_refresh_row(admin_client, session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))

    assert passmark_scraper.run_cpu_list_scraper() == 300

    assert len(session.exec(select(CPUBenchmark)).all()) == 300
    [row] = _refreshes(session, "cpu")
    assert row.row_count == 300
    assert row.source_updated_text == "Last updated on the 1st of November 2026"
    assert _refreshes(session, "gpu") == []


def test_a_gpu_run_writes_the_gpu_table_and_its_own_row(admin_client, session, monkeypatch, no_floor):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_gpu.html"))

    assert passmark_scraper.run_gpu_list_scraper() == 300

    assert len(session.exec(select(GPUBenchmark)).all()) == 300
    assert [r.row_count for r in _refreshes(session, "gpu")] == [300]


def test_a_second_run_updates_marks_and_adds_a_second_refresh_row(admin_client, session, monkeypatch, no_floor):
    table = '<table id="cputable"><tbody><tr><td>Chip X</td><td>{}</td></tr></tbody></table>'
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: table.format("100"))
    passmark_scraper.run_cpu_list_scraper()
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: table.format("250"))
    passmark_scraper.run_cpu_list_scraper()

    [chip] = session.exec(select(CPUBenchmark).where(CPUBenchmark.cpu_name == "Chip X")).all()
    session.refresh(chip)
    assert chip.cpu_mark == 250
    assert len(_refreshes(session, "cpu")) == 2


def test_a_failed_run_writes_nothing(admin_client, session, monkeypatch):
    # The real floor, and a page below it
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))

    with pytest.raises(ValueError, match="below the floor"):
        passmark_scraper.run_cpu_list_scraper()

    assert session.exec(select(CPUBenchmark)).all() == []
    assert _refreshes(session, "cpu") == []


def test_a_run_that_fails_while_writing_leaves_no_refresh_row(admin_client, session, monkeypatch, no_floor):
    # The row and the upsert share a transaction: no marks, no date
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _fixture("passmark_cpu.html"))
    monkeypatch.setattr(passmark_scraper, "BenchmarkRefresh", lambda **kw: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        passmark_scraper.run_cpu_list_scraper()

    assert session.exec(select(CPUBenchmark)).all() == []


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
