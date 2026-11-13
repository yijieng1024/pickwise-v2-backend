"""The weekly pipeline's run logic (plan step 11, items 8 and 10), with stand-in steps:
no database, no site, no LLM, no mail server."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.unit._adapters import pipeline_settings as ps
from tests.unit._adapters import bulk_scraper
from tests.unit._adapters import weekly_pipeline as wp

# Saturday 19:30 UTC is Sunday 03:30 in Malaysia, when the daily workflow wakes up.
SUNDAY_IN_MALAYSIA = datetime(2026, 11, 14, 19, 30, tzinfo=timezone.utc)


def test_a_scheduled_run_exits_when_the_pipeline_is_off():
    assert wp.not_due(ps.PipelineSettings(enabled=False), SUNDAY_IN_MALAYSIA, scheduled=True) == \
        "the pipeline is switched off in admin"


def test_a_scheduled_run_exits_on_another_weekday_and_reads_the_day_in_malaysia():
    reason = wp.not_due(ps.PipelineSettings(enabled=True, weekday="saturday"), SUNDAY_IN_MALAYSIA, scheduled=True)
    assert reason == "today is sunday in Malaysia, and the pipeline runs on saturday"


def test_a_scheduled_run_goes_ahead_on_its_weekday():
    assert wp.not_due(ps.PipelineSettings(enabled=True, weekday="sunday"), SUNDAY_IN_MALAYSIA, scheduled=True) is None


def test_a_manual_run_ignores_off_and_the_weekday():
    off_and_wrong_day = ps.PipelineSettings(enabled=False, weekday="monday")
    assert wp.not_due(off_and_wrong_day, SUNDAY_IN_MALAYSIA, scheduled=False) is None


class _Session:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def expunge_all(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.mark.parametrize("settings, called", [
    (ps.PipelineSettings(enabled=False), False),
    (ps.PipelineSettings(enabled=True, weekday="monday"), False),
    (ps.PipelineSettings(enabled=True, weekday="sunday"), True),
])
def test_main_leaves_no_job_record_unless_the_scheduled_run_is_due(monkeypatch, settings, called):
    calls = []
    monkeypatch.setattr(wp, "write_target", lambda variable: "sqlite://")
    monkeypatch.setattr(wp, "Session", _Session)
    monkeypatch.setattr(wp, "read_settings", lambda session: settings)
    monkeypatch.setattr(wp, "datetime", SimpleNamespace(now=lambda tz=None: SUNDAY_IN_MALAYSIA))
    monkeypatch.setattr(wp, "apply", lambda engine, s, scheduled: calls.append(scheduled) or {"job_id": "j"})
    monkeypatch.setattr("sys.argv", ["weekly_pipeline", "--apply", "--scheduled"])
    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    assert wp.main() == 0
    assert calls == ([True] if called else [])


def test_the_brand_switches_limit_the_crawl(monkeypatch):
    crawled = []

    async def fake_crawl(brand_id, session, hosts=None):
        crawled.append((brand_id, tuple(hosts)))
        return {"total_found": 1}

    monkeypatch.setattr(bulk_scraper, "run_feed_crawl", fake_crawl)
    monkeypatch.setattr(wp, "_brand_id", lambda session, name: f"id-{name}")
    run = wp.Run(_Session(), ps.PipelineSettings(asus_enabled=False, hp_enabled=False), SUNDAY_IN_MALAYSIA)

    assert wp.step_crawl(run) == {"asus": {"total_found": 1}}
    assert crawled == [("id-asus", ("rog.asus.com",))], "HP was crawled, or ASUS pages were queued"


def test_one_brand_whose_crawl_fails_is_recorded_and_the_others_still_run(monkeypatch):

    async def fake_crawl(brand_id, session, hosts=None):
        if brand_id == "id-asus":
            raise RuntimeError("blocked")
        return {"total_found": 3}

    monkeypatch.setattr(bulk_scraper, "run_feed_crawl", fake_crawl)
    monkeypatch.setattr(wp, "_brand_id", lambda session, name: f"id-{name}")
    out = wp.step_crawl(wp.Run(_Session(), ps.PipelineSettings(), SUNDAY_IN_MALAYSIA))
    assert out == {"asus": {"error": "RuntimeError: blocked"}, "hp": {"total_found": 3}}


def test_a_step_that_fails_as_a_whole_stops_the_run_and_says_where():
    ran = []

    def ok(name):
        return lambda run: ran.append(name) or {"done": name}

    def broken(run):
        raise ValueError("database gone")

    steps = (("crawl", ok("crawl")), ("scrape", broken), ("process", ok("process")), ("guides", ok("guides")))
    result = wp.run_steps(wp.Run(_Session(), ps.PipelineSettings(), SUNDAY_IN_MALAYSIA), steps)

    assert ran == ["crawl"]
    assert result == {"steps": {"crawl": {"done": "crawl"}}, "stopped_at": "scrape",
                      "error": "ValueError: database gone", "not_run": ["process", "guides"]}


def test_every_step_runs_when_none_fails_and_each_is_reported():
    seen = []
    steps = tuple((name, lambda run, n=name: {"step": n}) for name in ("a", "b", "c"))
    result = wp.run_steps(wp.Run(_Session(), ps.PipelineSettings(), SUNDAY_IN_MALAYSIA), steps, on_step=seen.append)
    assert result == {"steps": {"a": {"step": "a"}, "b": {"step": "b"}, "c": {"step": "c"}}, "stopped_at": None}
    assert seen == ["a", "b", "c"]


def _pick_scores(monkeypatch, results, changes):
    generated = []
    monkeypatch.setattr(wp, "price_changes", lambda session, since: changes)
    run = wp.Run(_Session(), ps.PipelineSettings(), SUNDAY_IN_MALAYSIA, results=results)
    out = wp.step_pick_scores(run, generate=lambda session: generated.append(1) or {"written": True})
    return out, generated


def test_pick_score_is_skipped_when_no_laptop_went_live_and_no_price_changed(monkeypatch):
    held_only = {"process": {"go_live": {"activated": [], "held": [{"laptop_id": "x"}]}},
                 "go_live": {"activated": []}}
    out, generated = _pick_scores(monkeypatch, held_only, [])
    assert out == {"skipped": "no laptop went live and no price changed"}
    assert generated == []


@pytest.mark.parametrize("results, changes", [
    ({"go_live": {"activated": [{"laptop_id": "x"}]}}, []),
    ({"process": {"go_live": {"activated": [{"laptop_id": "x"}]}}}, []),
    ({}, [{"laptop_id": "y", "old_price": 5000.0, "new_price": 4800.0}]),
])
def test_pick_score_runs_when_a_laptop_went_live_or_a_price_changed(monkeypatch, results, changes):
    out, generated = _pick_scores(monkeypatch, results, changes)
    assert out == {"written": True} and generated == [1]
