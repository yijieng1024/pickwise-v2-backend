"""reprocess_reviews run --apply stops before a review that would take the run
past --max-windows, and counts only windows not yet staged."""
import contextlib

from tests.unit._adapters import reprocess_reviews as rr


def test_stops_before_the_review_that_would_exceed_the_cap(monkeypatch):
    # three reviews of 10 windows; v1 already has 4 staged, so it costs 6
    rows = [(f"id{n}", f"v{n}", "t", [None] * 10, []) for n in (1, 2, 3)]
    processed = []
    monkeypatch.setattr(rr, "Session", lambda engine: contextlib.nullcontext(None))
    monkeypatch.setattr(rr, "get_engine", lambda: None)
    monkeypatch.setattr(rr, "_processable", lambda session, vids: rows)
    monkeypatch.setattr(rr.processor, "_saved_windows", lambda s, vids, staging: {"v1": {0, 1, 2, 3}})
    monkeypatch.setattr(rr.processor, "windows_with_context", lambda seg: [{"start": i} for i in range(len(seg))])

    def fake_process(rid, session, stage_only):
        processed.append(rid)
        return {"chunks_saved": 1, "chunks_failed": 0, "complete": True,
                "windows_already_saved": 0, "failures": []}

    monkeypatch.setattr(rr.processor, "process_raw_review", fake_process)
    rr.run_apply(None, None, max_windows=16)  # 6 + 10 fits, + 10 more does not
    assert processed == ["id1", "id2"]
