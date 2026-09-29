"""
The PickScore scoring cache (app/pickscore/ranges_cache.py): a request is
served the stale copy while one background thread rebuilds, a rebuild that
raced a write is not filed as current, and committed writes to the source
tables - ORM flushes and Core upserts alike - invalidate it.

The listener tests use in-memory SQLite with only the benchmark table: the
events fire the same way, and no real database is touched.
"""

import threading

import pytest
from sqlalchemy import create_engine, insert
from sqlmodel import Session

from ._adapters import CPUBenchmark, ranges_cache  # importing _adapters registers the listeners


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    for name, value in (("_data", None), ("_built_at", 0.0), ("_built_generation", -1),
                        ("_generation", 0), ("_refreshing", False)):
        monkeypatch.setattr(ranges_cache, name, value)


def wait_for_rebuild():
    for t in threading.enumerate():
        if t.name != threading.current_thread().name and t.daemon:
            t.join(timeout=5)


def test_nothing_cached_means_caller_builds_and_no_thread_starts():
    assert ranges_cache.get(lambda: pytest.fail("must not rebuild in the background")) is None


def test_stale_copy_is_served_while_one_rebuild_runs():
    ranges_cache.store("old", ranges_cache.generation())
    ranges_cache.invalidate()
    release, calls = threading.Event(), []

    def build():
        calls.append(1)
        release.wait(5)
        return "new"

    assert ranges_cache.get(build) == "old"
    assert ranges_cache.get(build) == "old"  # second request: no second rebuild
    release.set()
    wait_for_rebuild()
    assert calls == [1]
    assert ranges_cache.get(build) == "new"


def test_rebuild_that_raced_a_write_stays_stale():
    ranges_cache.store("old", ranges_cache.generation())
    ranges_cache.invalidate()

    def build():
        ranges_cache.invalidate()  # a write commits while the rebuild reads
        return "built-from-old-rows"

    ranges_cache.get(build)
    wait_for_rebuild()
    rebuilds = []
    assert ranges_cache.get(lambda: rebuilds.append(1) or "fresh") == "built-from-old-rows"
    wait_for_rebuild()
    assert rebuilds == [1]  # still stale, so the next request rebuilt again


@pytest.fixture
def sqlite_session():
    engine = create_engine("sqlite://")
    CPUBenchmark.__table__.create(engine)
    with Session(engine) as session:
        yield session


def test_orm_write_invalidates_only_on_commit(sqlite_session):
    before = ranges_cache.generation()
    sqlite_session.add(CPUBenchmark(cpu_name="Apple M5", cpu_mark=30000))
    sqlite_session.flush()
    assert ranges_cache.generation() == before  # not yet committed
    sqlite_session.commit()
    assert ranges_cache.generation() == before + 1


def test_core_upsert_invalidates(sqlite_session):
    # The benchmark scrapers write with Core insert(...).on_conflict_do_update.
    before = ranges_cache.generation()
    sqlite_session.exec(insert(CPUBenchmark).values(cpu_name="Apple M5", cpu_mark=30000))
    sqlite_session.commit()
    assert ranges_cache.generation() == before + 1


def test_commit_without_source_writes_leaves_cache_alone(sqlite_session):
    before = ranges_cache.generation()
    sqlite_session.commit()
    assert ranges_cache.generation() == before
