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

import logging
import uuid

import pytest

from ._adapters import (
    UserConstraints,
    _relational_fallback,
    _retrieval,
    _run_search,
    _search_tool,
    gate_threshold,
    min_viable_score,
    relevance_gate,
    rerank,
)
from .test_pipeline_internals import _FakeLaptop, _install_stubs


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

    def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rows(n=3):
    return [
        (_FakeLaptop(model_code=f"FB{i}", price_rm=2000.0 + i, family_id=None), "Asus")
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


def test_fallback_still_hardcodes_a_similarity_of_one_half():
    """
    Pins the number the rest of this file reasons about. The fallback builds
    RetrievalCandidate the same way the pgvector path does — there is no second
    constructor — but with a literal cosine_distance of 0.5, so the derived
    similarity_score is 0.5 for every row regardless of the query.
    """
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    assert candidates
    assert all(c.cosine_distance == 0.5 for c in candidates)
    assert all(c.similarity_score == pytest.approx(0.5) for c in candidates)


def test_the_fallback_constant_is_below_the_gate():
    """
    The whole finding in one line. Two individually reasonable constants: a
    neutral 0.5 placeholder, and a 0.53 gate calibrated so irrelevant queries
    are blocked. 0.5 < 0.53, so the placeholder is permanently on the blocked
    side of a threshold that was never calibrated against it.
    """
    assert 0.5 < gate_threshold()


def test_fallback_scores_are_viable_so_relaxation_never_runs():
    """
    0.5 clears _MIN_VIABLE_SCORE (0.33), so needs_relaxation is False and the
    pipeline walks straight from rerank to the gate. There is no intermediate
    stage that could notice the pool is synthetic.
    """
    assert 0.5 > min_viable_score()


# --------------------------------------------------------------------------
# What the gate does with it
# --------------------------------------------------------------------------


def test_unconstrained_fallback_candidates_are_gated():
    """
    Catches the defence-cancelling-defence class: a rescue path whose output
    cannot clear the filter downstream of it. With no constraints at all — the
    most favourable case, penalty 1.0, no bonus — the top score is exactly 0.5
    and the gate blocks it.
    """
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    ranked = rerank(candidates, UserConstraints())
    assert ranked[0].final_score == pytest.approx(0.5)
    assert relevance_gate(ranked).status == "gated"


def test_a_penalty_only_makes_it_worse():
    """Every constraint the user states pushes the fallback further under the
    gate. There is no input that rescues it."""
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    ranked = rerank(candidates, UserConstraints(budget=1500.0))
    assert ranked[0].final_score < 0.5
    assert relevance_gate(ranked).status == "gated"


def test_only_a_brand_bonus_could_ever_lift_it_over():
    """
    The one arithmetic escape, recorded so the finding is exact rather than
    absolute: +0.05 for a matching brand preference puts a fallback row at 0.55,
    over the gate. It needs the user to have named a brand AND that brand to
    win the price sort, so it is a coincidence, not a rescue — and it means a
    fallback search can return results for one user and a clarifying question
    for another on the same broken API.
    """
    candidates = _relational_fallback(_FakeSession(_rows()), None, None, 50)
    ranked = rerank(candidates, UserConstraints(brand_preferences=["asus"]))
    assert ranked[0].final_score == pytest.approx(0.55)
    assert relevance_gate(ranked).status == "pass"


# --------------------------------------------------------------------------
# End to end: what the user receives
# --------------------------------------------------------------------------


def test_user_gets_a_clarifying_question_not_laptops_when_embedding_is_down(monkeypatch):
    """
    The question that matters. The embedding API is down, the fallback fetches
    real laptops from SQL, and the user is told to loosen their requirements.

    The clarifying question is about the user's budget or brand, which is the
    misleading part: nothing they can change will help, because the catalog was
    never searched.
    """
    session = _FakeSession(_rows(5))
    _break_the_embedding(monkeypatch)
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: session)

    payload = _run_search("a light laptop for programming")

    assert payload["results"] == []
    assert payload["confidence"] == "low"
    assert payload["message"]
    assert payload["bottleneck"] == "general"


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


def test_nothing_records_that_the_search_was_a_fallback(monkeypatch):
    """
    An invisible degraded mode is the same class of problem as the gate
    interaction itself: if it cannot be seen in the logs, nobody learns the
    embedding API is down from anything except the gated replies.

    Asserted as it is, not as it should be. retrieve_candidates swallows the
    exception with a bare `except Exception:` and no log call, and
    log_pipeline_result's record has no retrieval-mode field — gate_status,
    top_score, bottleneck, relaxed_*, candidate_count, result_laptop_ids. A
    fallback run is indistinguishable from a genuine near-miss except by
    noticing top_score is exactly 0.5.

    When a marker is added, this test goes red and gets rewritten to assert it.
    """
    logged = {}

    def _capture(**kwargs):
        logged.update(kwargs)

    session = _FakeSession(_rows(3))
    _break_the_embedding(monkeypatch)
    _install_stubs(monkeypatch)
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **kw: session)
    monkeypatch.setattr(_search_tool, "log_pipeline_result", _capture)

    _run_search("a light laptop")

    assert logged, "the pipeline logger was called"
    assert set(logged) == {"gate", "query", "relaxation", "session"}
    assert logged["relaxation"] is None
    # The only trace of the outage anywhere in the record:
    assert logged["gate"].top_score == pytest.approx(0.5)
