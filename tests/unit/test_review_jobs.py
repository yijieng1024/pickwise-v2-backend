"""Review background-job workers, the Google quota day, embedding usage
logging and the Laptop write event. No database: the workers' slow calls and
reads are replaced, and the session is a stub."""
import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import event

from tests._progress import RecordingProgress

from ._adapters import embeddings_service, laptop_models, llm_usage_module, reprocess_reviews, review_jobs

llm_usage = llm_usage_module()


class _Session:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        pass


def _report(saved, failed, failures=()):
    return {
        "chunks_total": saved + failed, "chunks_saved": saved, "chunks_failed": failed,
        "failures": list(failures), "complete": failed == 0,
    }


def test_process_counts_done_partial_failed_and_skipped(monkeypatch):
    ids = [uuid.uuid4() for _ in range(4)]
    window_error = {"error_type": "LLMUnavailable", "error": "429"}
    outcomes = {
        ids[0]: _report(5, 0),
        ids[1]: _report(3, 2, [window_error]),
        ids[2]: RuntimeError("transcript gone"),
        ids[3]: review_jobs.AlreadyProcessed("every window saved"),
    }

    def fake_process(review_id, _session):
        out = outcomes[review_id]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(review_jobs, "process_raw_review", fake_process)
    monkeypatch.setattr(
        review_jobs, "processing_candidates",
        lambda _s: [{"review_id": rid, "video_title": f"t{i}"} for i, rid in enumerate(ids)],
    )
    session, progress = _Session(), RecordingProgress()

    out = review_jobs.process_bulk_worker(10)(session, progress)

    assert (out["done"], out["failed"], out["skipped"]) == (1, 2, 1)
    assert (out["chunks_saved"], out["chunks_failed"]) == (8, 2)
    assert progress.total == 4
    # A partial review is a failure, named by review id, with the first window's error.
    assert progress.errors == [
        {"item": str(ids[1]), "error": "2 of 5 windows failed: LLMUnavailable: 429"},
        {"item": str(ids[2]), "error": "RuntimeError: transcript gone"},
    ]
    assert session.rollbacks == 2  # an exception never leaves the transaction aborted


def test_process_bulk_respects_limit_and_stops_on_cancel(monkeypatch):
    ids = [uuid.uuid4() for _ in range(5)]
    seen = []
    monkeypatch.setattr(review_jobs, "process_raw_review", lambda rid, _s: seen.append(rid) or _report(1, 0))
    monkeypatch.setattr(
        review_jobs, "processing_candidates",
        lambda _s: [{"review_id": rid, "video_title": "t"} for rid in ids],
    )

    review_jobs.process_bulk_worker(3)(_Session(), RecordingProgress(cancel_after=2))

    assert seen == ids[:2]


def test_single_process_worker_runs_only_that_review(monkeypatch):
    rid = uuid.uuid4()
    seen = []
    monkeypatch.setattr(review_jobs, "process_raw_review", lambda r, _s: seen.append(r) or _report(2, 0))

    out = review_jobs.process_one_worker(rid, "TUF F16 review")(_Session(), RecordingProgress())

    assert seen == [rid]
    assert out["done"] == 1 and out["results"][0]["video_title"] == "TUF F16 review"


def test_quota_day_resets_at_pacific_midnight_across_dst():
    # Summer: midnight PDT is 07:00 UTC.
    start, end = llm_usage.quota_day(datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc))
    assert (start, end) == (
        datetime(2026, 6, 30, 7, tzinfo=timezone.utc), datetime(2026, 7, 1, 7, tzinfo=timezone.utc)
    )
    # Winter: midnight PST is 08:00 UTC.
    start, _ = llm_usage.quota_day(datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc))
    assert start == datetime(2026, 12, 1, 8, tzinfo=timezone.utc)
    # The day DST ends (2026-11-01) is 25 hours long.
    start, end = llm_usage.quota_day(datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    assert (end - start).total_seconds() == 25 * 3600


def test_embed_text_logs_one_embedding_call(monkeypatch):
    calls = []
    monkeypatch.setattr(embeddings_service, "_embedder", SimpleNamespace(embed_query=lambda t: [0.1] * 768))
    monkeypatch.setattr(embeddings_service, "log_llm_usage", lambda *a: calls.append(a))

    embeddings_service.embed_text("tuf f16")

    assert calls == [(llm_usage.LLMFeature.EMBEDDING, embeddings_service.EMBEDDING_MODEL, 0, 0)]


def test_failed_embedding_logs_nothing(monkeypatch):
    calls = []

    def boom(_t):
        raise TimeoutError

    monkeypatch.setattr(embeddings_service, "_embedder", SimpleNamespace(embed_query=boom))
    monkeypatch.setattr(embeddings_service, "log_llm_usage", lambda *a: calls.append(a))

    try:
        embeddings_service.embed_text("x")
    except TimeoutError:
        pass
    assert calls == []


def test_reprocess_dry_run_records_usage():
    # The dry run writes no claims but spends real quota.
    assert "feature=None" not in inspect.getsource(reprocess_reviews.run_dry)


def test_laptop_write_event_stamps_updated_at_and_still_demotes_unpriced():
    fn = laptop_models._deactivate_unpriced
    assert event.contains(laptop_models.Laptop, "before_insert", fn)
    assert event.contains(laptop_models.Laptop, "before_update", fn)

    before = datetime.now(timezone.utc)
    unpriced = SimpleNamespace(price_rm=0, status="active", updated_at=None)
    fn(None, None, unpriced)
    assert unpriced.status == "inactive" and unpriced.updated_at >= before

    priced = SimpleNamespace(price_rm=4999.0, status="active", updated_at=None)
    fn(None, None, priced)
    assert priced.status == "active" and priced.updated_at >= before
