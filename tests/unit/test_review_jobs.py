"""Review background-job workers and the Laptop write event. No database:
the workers' slow calls and reads are replaced, and the session is a stub."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import event

from tests._progress import RecordingProgress

from ._adapters import laptop_models, review_jobs


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
