"""
One row per model call of an agent turn, failed calls included, captured by
MonitoringCallbackHandler from the real model class against the fake Gemini
server. tests/integration/test_agent_model_calls.py holds the rows in Postgres.
"""

import pytest

from ._adapters import monitoring_service
from .test_llm_fallback import _B, _P, gemini, llm_settings  # noqa: F401 - fixtures
from .test_sticky_fallback import ROUTES, _Sequence, _tool_steps, turn  # noqa: F401 - fixture


def _rows(handler):
    return [(c["step"], c["seq"], c["model"], c["chain_position"], c["is_fallback"], c["status"])
            for c in handler.model_calls]


@pytest.mark.parametrize("route", ROUTES)
def test_a_failed_call_and_its_fallback_share_a_step_in_order(gemini, turn, route):  # noqa: F811
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    _tool_steps(gemini, 1)
    handler = monitoring_service.MonitoringCallbackHandler()

    turn(route, handler)

    assert _rows(handler) == [
        (1, 1, "primary-model", 0, False, "error"),
        (1, 2, "backup-model", 1, True, "answered"),
        (2, 3, "backup-model", 1, True, "answered"),
    ]
    failed, tool_step, last = handler.model_calls
    assert failed["error_type"] == "LLMUnavailable" and "429" in failed["error_message"]
    assert (failed["input_tokens"], failed["output_tokens"], failed["tool_calls"]) == (None, None, [])
    assert tool_step["tool_calls"] == ["lookup"] and tool_step["input_tokens"]
    assert last["tool_calls"] == []
    assert tool_step["error_type"] is None
    assert all(c["duration_ms"] is not None and c["started_at"] for c in handler.model_calls)
    assert failed["started_at"] <= tool_step["started_at"] <= last["started_at"]
    assert handler.fallback_triggered is True


def test_a_backup_that_was_started_but_failed_is_not_a_fallback(gemini, turn):  # noqa: F811
    # Run b6471466's shape: the primary answered a step, a later step's backup
    # returned a 400. The old flag said a backup had answered.
    gemini.behaviour = _Sequence({"primary-model": ["ok", "429"], "backup-model": ["400"]})
    _tool_steps(gemini, 1)
    handler = monitoring_service.MonitoringCallbackHandler()

    with pytest.raises(Exception):
        turn("run_agent", handler)

    assert _rows(handler) == [
        (1, 1, "primary-model", 0, False, "answered"),
        (2, 2, "primary-model", 0, False, "error"),
        (2, 3, "backup-model", 1, True, "error"),
    ]
    assert handler.fallback_triggered is False
    assert (handler.model, handler.model_position) == ("primary-model", 0)


def test_a_turn_on_the_primary_alone_is_not_a_fallback(gemini, turn):  # noqa: F811
    gemini.behaviour.update({"primary-model": "ok", "backup-model": "ok"})
    handler = monitoring_service.MonitoringCallbackHandler()

    turn("stream_agent", handler)

    assert _rows(handler) == [(1, 1, "primary-model", 0, False, "answered")]
    assert handler.fallback_triggered is False


def test_a_call_that_never_finished_is_closed_as_an_error():
    import asyncio
    import uuid

    handler = monitoring_service.MonitoringCallbackHandler()
    asyncio.run(handler.on_chat_model_start({}, [], run_id=uuid.uuid4(), metadata={"ls_model_name": "m"}))

    handler.finalize_incomplete_calls()

    [call] = handler.model_calls
    assert (call["status"], call["error_type"]) == ("error", "Unfinished")
    assert call["duration_ms"] is not None
