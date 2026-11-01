"""
The agent_model_calls rows of a real turn: the real model class against the
fake Gemini server, the real callback handler, real Postgres, and the admin
endpoint that reads them back.
"""

from sqlmodel import select

from app.agent.monitoring_models import AgentModelCall, AgentRunLog
from app.agent.monitoring_service import MonitoringCallbackHandler, log_agent_run
from app.rag.models import Conversation
from tests.unit.test_llm_fallback import _B, _P, gemini, llm_settings  # noqa: F401 - fixtures
from tests.unit.test_sticky_fallback import _tool_steps, turn  # noqa: F401 - fixture


def _log_turn(session, user, handler, status="success"):
    conversation = Conversation(user_id=user.id, title="t")
    session.add(conversation)
    session.commit()
    log_agent_run(conversation.id, user.id, "hi", "reply", status, None, 10, handler)
    return session.exec(select(AgentRunLog).where(AgentRunLog.conversation_id == conversation.id)).one()


def test_a_failed_primary_then_a_backup_writes_two_rows_in_order(
    admin_client, admin_user, session, gemini, turn,  # noqa: F811
):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    handler = MonitoringCallbackHandler()
    turn("stream_agent", handler)

    run = _log_turn(session, admin_user, handler)

    rows = session.exec(
        select(AgentModelCall).where(AgentModelCall.run_id == run.id).order_by(AgentModelCall.seq)
    ).all()
    assert [(r.step, r.model, r.chain_position, r.is_fallback, r.status) for r in rows] == [
        (1, "primary-model", 0, False, "error"),
        (1, "backup-model", 1, True, "answered"),
    ]
    assert rows[0].error_type == "LLMUnavailable" and "429" in rows[0].error_message
    assert rows[0].input_tokens is None
    assert rows[1].error_type is None and rows[1].input_tokens == 7 and rows[1].output_tokens == 3
    assert rows[0].started_at <= rows[1].started_at
    assert run.fallback_triggered is True


def test_the_run_detail_lists_the_calls_in_step_order_with_their_tool_calls(
    admin_client, admin_user, session, gemini, turn,  # noqa: F811
):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    _tool_steps(gemini, 1)
    handler = MonitoringCallbackHandler()
    turn("run_agent", handler)
    run = _log_turn(session, admin_user, handler)

    response = admin_client.get(f"/api/v2/agent/monitoring/runs/{run.id}")

    assert response.status_code == 200
    calls = response.json()["model_calls"]
    assert [(c["step"], c["seq"], c["model"], c["status"], c["tool_calls"]) for c in calls] == [
        (1, 1, "primary-model", "error", []),
        (1, 2, "backup-model", "answered", ["lookup"]),
        (2, 3, "backup-model", "answered", []),
    ]


def test_a_run_from_before_the_table_has_an_empty_list(admin_client, admin_user, session):
    run = _log_turn(session, admin_user, None)

    assert admin_client.get(f"/api/v2/agent/monitoring/runs/{run.id}").json()["model_calls"] == []
