"""
What a user gets when the embedding API is down.

retrieve_candidates catches any failure from the Gemini embedding call and
returns _relational_fallback's price-ordered list instead, so the conversation
"never stalls completely". The gate then checks the top candidate against
RELEVANCE_THRESHOLD.

These two guards were written separately and neither knows about the other.
This file measures what they do together. No database: the fallback's session
is faked at the one call it makes (session.execute(stmt).all()), and the
statement it builds is never executed.
"""

import json
import logging
import uuid

import pytest

from ._adapters import (
    UserConstraints,
    _real_log_pipeline_result,
    _relational_fallback,
    _retrieval,
    _run_search,
    _search_tool,
    fallback_similarity,
    gate_threshold,
    min_viable_score,
    relevance_gate,
    rerank,
)
from .test_pipeline_internals import _FakeLaptop, _candidate as _pgvector_candidate, _install_stubs


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Answers the single `session.execute(stmt).all()` the fallback makes. The
    SELECT is still built for real; it is just never sent anywhere."""

    def __init__(self, rows):
        self._rows = rows
        self.statements = []
        self.added = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._rows)

    # log_pipeline_result's DB half runs against this too, so the
    # PipelineEvalLog row is really constructed -- which is what proves the
    # model accepts the new column.
    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rows(n=3, **laptop_kw):
    return [
        (
            _FakeLaptop(
                model_code=f"FB{i}", price_rm=2000.0 + i, family_id=None, **laptop_kw
            ),
            "Asus",
        )
        for i in range(n)
    ]


def _break_the_embedding(monkeypatch):
    """Make the embedding call fail the way an API outage does."""

    def _boom(query):
        raise RuntimeError("embedding API unavailable")

    monkeypatch.setattr(_retrieval, "_get_query_vector", _boom)


# --------------------------------------------------------------------------
# The constant
# --------------------------------------------------------------------------


def test_every_fallback_row_carries_the_placeholder_similarity():
    """
    The fallback builds RetrievalCandidate the same way the pgvector path does
    — there is no second constructor — but with a fixed distance, so every row
    scores the same regardless of the query. That is the point: the number is a
    placeholder, and from_fallback is what says so.
    """
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    assert candidates
    assert all(
        c.similarity_score == pytest.approx(fallback_similarity()) for c in candidates
    )
    assert all(c.from_fallback for c in candidates)


def test_the_fallback_constant_clears_the_gate():
    """
    THE REGRESSION TEST for the defence-cancelling-defence bug. The placeholder
    was 0.5 against a 0.53 gate, so every fallback result was blocked and the
    rescue path rescued nothing. Two individually reasonable constants that
    annihilated each other because neither was calibrated against the other.
    """
    assert fallback_similarity() > gate_threshold()


def test_the_fallback_constant_sorts_below_genuine_matches():
    """
    The other half of the derivation, and the reason the answer is not "raise it
    until the end-to-end test goes green". Measured over the 241 rows in
    pipeline_eval_logs on 2026-09-14: p10 0.5867, p25 0.6270, p50 0.6778. The
    constant must sit under the bottom of that distribution, or a placeholder
    outranks a real semantic match wherever the two meet.
    """
    measured_p10, measured_p25 = 0.5867, 0.6270
    assert fallback_similarity() < measured_p10 < measured_p25


def test_a_genuine_hit_outranks_a_fallback_row():
    """The ordering consequence, asserted rather than assumed."""
    fallback = _relational_fallback(_FakeSession(_rows(1)), None, None, 50)
    genuine = [_pgvector_candidate(similarity=0.6270)]
    ranked = rerank(fallback + genuine, UserConstraints())
    assert ranked[0].similarity_score == pytest.approx(0.6270)


def test_fallback_scores_are_viable_so_relaxation_never_runs():
    """
    The placeholder clears _MIN_VIABLE_SCORE (0.33), so needs_relaxation is
    False and the pipeline walks straight from rerank to the gate. There is no
    intermediate stage that could notice the pool is synthetic — which is why
    the flag had to be explicit rather than inferred.
    """
    assert fallback_similarity() > min_viable_score()


# --------------------------------------------------------------------------
# What the gate does with it
# --------------------------------------------------------------------------


def test_unconstrained_fallback_candidates_pass_the_gate():
    """The plain case: embedding down, no constraints, penalty 1.0, no bonus.
    The user gets laptops."""
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    ranked = rerank(candidates, UserConstraints())
    assert ranked[0].final_score == pytest.approx(fallback_similarity())
    assert relevance_gate(ranked).status == "pass"


def test_a_stated_budget_does_not_gate_the_fallback():
    """
    The budget penalty cannot fire on fallback rows: _relational_fallback
    applies `price_rm <= budget_max` in SQL, so everything it returns is already
    inside budget. Worth pinning — it is why one constant was enough for the
    budget case, and why the weight case below still is not.
    """
    candidates = _relational_fallback(_FakeSession(_rows()), 5000.0, None, 50)
    ranked = rerank(candidates, UserConstraints(budget=5000.0))
    assert relevance_gate(ranked).status == "pass"


def test_a_weight_constrained_fallback_is_not_gated():
    """
    THE REGRESSION TEST for the half-finished part of Change 1. This asserted
    the gated outcome, because there was no SQL weight filter and the surviving
    rows took the x0.7 penalty: 0.58 x 0.7 = 0.406, under the gate. Surviving
    that multiplier needs a constant above 0.757, past p75 of genuine hits, so
    no placeholder value could fix it.

    The fallback filters weight in SQL now, so every row it returns is already
    inside the limit and the penalty is a no-op on this path. That the database
    really excludes the heavier rows is asserted against a real Postgres in
    tests/integration/test_retrieval_filters.py; what is asserted here is the
    composition -- given rows within the limit, nothing downstream re-penalises
    them back under the gate.
    """
    session = _FakeSession(_rows(weight_kg=0.9))
    candidates = _relational_fallback(session, None, None, 50, weight_max=1.0)
    ranked = rerank(candidates, UserConstraints(weight_limit=1.0))
    assert ranked[0].penalty_multiplier == pytest.approx(1.0)
    assert relevance_gate(ranked).status == "pass"


def test_the_weight_filter_reaches_the_sql_not_the_penalty():
    """
    Catches the filter being applied in Python after the query, or not at all.
    The point of the change is that the relational path excludes what it can
    exclude rather than retrieving it and demoting it — a weight penalty is a
    semantic-retrieval remedy, and there is nothing semantic about this query.
    """
    session = _FakeSession(_rows())
    _relational_fallback(session, None, None, 50, weight_max=1.0)
    rendered = str(session.statements[0]).lower()
    # The bound, not just the column name -- weight_kg is in the SELECT list
    # either way, so `"weight_kg" in rendered` would pass with no filter at all.
    assert "weight_kg <=" in rendered


def test_the_filter_is_a_plain_bound_with_no_null_escape_hatch():
    """
    Matched to the existing budget filter, deliberately: a bare `weight_kg <=`
    with no `OR weight_kg IS NULL`.

    Both columns are NOT NULL on `laptops` (asserted in
    tests/integration/test_status_filtering.py), so the NULL case does not
    arise today. If the column is ever made nullable, SQL drops those rows —
    which is the behaviour to want: an unknown weight is not evidence that a
    laptop is light, and this is already the degraded path.
    """
    session = _FakeSession(_rows())
    _relational_fallback(session, None, None, 50, weight_max=1.0)
    rendered = str(session.statements[0]).lower()
    assert "weight_kg <=" in rendered
    assert "or weight_kg is null" not in rendered


def test_a_brand_preference_no_longer_changes_the_outcome():
    """
    The escape hatch is closed. Under the old constant a matching brand
    preference added +0.05 and lifted 0.5 to 0.55, so a fallback search returned
    results for a user who had named a brand and a clarifying question for one
    who had not, on the same broken API. Both now pass.
    """
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    with_brand = rerank(candidates, UserConstraints(brand_preferences=["asus"]))
    without = rerank(candidates, UserConstraints())
    assert relevance_gate(with_brand).status == "pass"
    assert relevance_gate(without).status == "pass"


# --------------------------------------------------------------------------
# End to end: what the user receives
# --------------------------------------------------------------------------


def test_user_gets_laptops_when_the_embedding_api_is_down(monkeypatch):
    """
    The question that matters, end to end. The embedding API is down, the
    fallback fetches real laptops from SQL, and the user receives them — rather
    than a clarifying question asking them to loosen requirements that were
    never what blocked the search.
    """
    session = _FakeSession(_rows(5))
    _break_the_embedding(monkeypatch)
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: session)

    payload = _run_search("a laptop for programming")

    assert payload["results"], "the fallback rows were discarded downstream"
    assert payload["confidence"] == "high"
    assert payload["bottleneck"] is None


def test_weight_limited_user_also_gets_laptops_when_the_api_is_down(monkeypatch):
    """
    The end-to-end case Change 1 should have been verified against. Its
    acceptance ran a search with no weight limit, which passed; adding one
    reverted the whole behaviour, and 1b made that invisible — a thrown-away
    fallback logs as an ordinary gate on a run flagged retrieval_fallback true.
    """
    logged = {}
    session = _FakeSession(_rows(5, weight_kg=0.9))
    _break_the_embedding(monkeypatch)
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: session)
    monkeypatch.setattr(
        _search_tool, "log_pipeline_result", lambda **kw: logged.update(kw)
    )

    payload = _run_search("a light laptop for programming", weight_max=1.0)

    assert payload["results"], "weight-limited fallback rows were discarded again"
    assert payload["confidence"] == "high"
    assert logged["retrieval_fallback"] is True
    # The bound reached the query. That the DATABASE then excludes heavier rows
    # is a database behaviour and is asserted against a real Postgres in
    # tests/integration/test_retrieval_filters.py -- _FakeSession returns its
    # rows whatever the WHERE clause says, so asserting exclusion here would
    # pass without a filter at all.
    assert "weight_kg <=" in str(session.statements[0]).lower()


def test_the_weight_limit_reaches_the_fallback_query(monkeypatch):
    """The tool has to pass weight_max down for any of the above to happen; it
    previously had no reason to, since only the reranker consumed it."""
    seen = {}

    def _spy(query, session, budget_max=None, weight_max=None, **kw):
        seen["weight_max"] = weight_max
        # Non-empty: an empty pool sends the tool into relaxation, which
        # re-enters retrieval through its own module-level name.
        return [_pgvector_candidate(0.80, weight_kg=0.9)]

    _install_stubs(monkeypatch, retrieve=_spy)
    _run_search("a light laptop", weight_max=1.2)
    assert seen["weight_max"] == 1.2


def test_fallback_did_run_and_did_find_laptops(monkeypatch):
    """
    Distinguishes "the fallback found nothing" from "the fallback found rows
    and they were discarded downstream". It is the second one — which is why
    raising the constant would be a fix at all.
    """
    session = _FakeSession(_rows(5))
    _break_the_embedding(monkeypatch)

    candidates = _retrieval.retrieve_candidates("a light laptop", session)

    assert len(candidates) == 5
    assert session.statements, "the fallback SELECT was built and executed"


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


def test_embedding_failure_is_logged_at_error_with_the_exception(monkeypatch, caplog):
    """
    Catches a silent degraded mode: the semantic search layer being down must
    reach the logs by itself, not only as a rise in gated replies.

    ERROR, not WARNING — pgvector search is the product, and a fallback to a
    price-ordered SQL list is not a degraded nicety. The exception is passed to
    the logger (exc_info) rather than formatted into the message, so the
    traceback survives to whoever reads the log.
    """
    _break_the_embedding(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="app.rag.retrieval"):
        _retrieval.retrieve_candidates("a light laptop", _FakeSession(_rows()))

    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records, "no ERROR record emitted when the embedding call failed"
    record = records[0]
    assert record.exc_info is not None, "exception not passed to the logger"
    assert record.exc_info[1].args[0] == "embedding API unavailable"


class _TraceCapture(logging.Handler):
    """The pickwise.eval logger sets propagate = False and owns its own
    FileHandler, so caplog cannot see it. Attach directly instead."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(json.loads(record.getMessage()))


def _run_and_capture_trace(monkeypatch, break_embedding: bool):
    """Run a real search and return the JSON lines written to
    logs/eval/pipeline_trace.jsonl — the written record, not a return value."""
    session = _FakeSession(_rows(3))
    if break_embedding:
        _break_the_embedding(monkeypatch)
    else:
        monkeypatch.setattr(_retrieval, "_get_query_vector", lambda q: [0.1] * 768)
        monkeypatch.setattr(
            _search_tool,
            "retrieve_candidates",
            lambda query, session, **kw: [
                _pgvector_candidate(0.80), _pgvector_candidate(0.70)
            ],
        )
    # log_pipeline_result is deliberately NOT stubbed here — it is the thing
    # under test.
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_search_tool, "log_pipeline_result", _real_log_pipeline_result)
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: session)

    capture = _TraceCapture()
    trace_logger = logging.getLogger("pickwise.eval")
    trace_logger.addHandler(capture)
    try:
        _run_search("a light laptop")
    finally:
        trace_logger.removeHandler(capture)
    return capture.records, session


def test_a_fallback_run_is_marked_in_the_trace(monkeypatch):
    """
    Catches an invisible degraded mode. Inferring a fallback from
    top_score == 0.5 stopped working the moment the constant moved, and it
    always collided with a genuine embedding hit landing on 0.5. The flag says
    which retrieval path ran, independently of what it scored.
    """
    records, session = _run_and_capture_trace(monkeypatch, break_embedding=True)
    assert len(records) == 1
    assert records[0]["retrieval_fallback"] is True
    assert session.added[0].retrieval_fallback is True, "the DB row must carry it too"


def test_a_normal_run_is_marked_false(monkeypatch):
    """The negative half: a flag that is always true reports nothing."""
    records, session = _run_and_capture_trace(monkeypatch, break_embedding=False)
    assert len(records) == 1
    assert records[0]["retrieval_fallback"] is False
    assert session.added[0].retrieval_fallback is False


def test_the_search_tool_tells_the_logger_which_path_ran(monkeypatch):
    """
    The flag has to be threaded from retrieval to the logger, and the tool is
    the only place that sees both. Asserted at the call boundary so it fails if
    someone reverts to deriving it from top_score.
    """
    logged = {}

    session = _FakeSession(_rows(3))
    _break_the_embedding(monkeypatch)
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: session)
    monkeypatch.setattr(_search_tool, "log_pipeline_result", lambda **kw: logged.update(kw))

    _run_search("a light laptop")

    assert logged["retrieval_fallback"] is True
