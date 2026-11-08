"""
Deleting a conversation that Pico replied in. Every FK to conversations is NO
ACTION, and the run logs used to be left behind, so this was an IntegrityError
500 for every conversation with a reply.
"""
from sqlmodel import select

from app.agent.monitoring_models import AgentModelCall, AgentRunLog
from app.rag.models import Conversation, Message, PipelineEvalLog


def test_a_conversation_with_run_logs_and_eval_logs_deletes(api_client, api_user, session):
    conv = Conversation(user_id=api_user.id, title="t")
    session.add(conv)
    session.flush()
    run = AgentRunLog(
        conversation_id=conv.id, user_id=api_user.id, user_message="hi", reply_text="reply",
        status="success", latency_ms=10, tool_calls=[],
    )
    session.add_all([run, Message(conversation_id=conv.id, role="user", content="hi")])
    session.flush()
    session.add(AgentModelCall(
        run_id=run.id, seq=1, step=1, model="m", chain_position=0, is_fallback=False,
        status="answered", started_at=run.created_at, duration_ms=1,
    ))
    eval_log = PipelineEvalLog(user_id=api_user.id, conversation_id=conv.id, query="q", gate_status="pass")
    session.add(eval_log)
    session.commit()
    conv_id, eval_id = conv.id, eval_log.id

    response = api_client.delete(f"/api/v2/conversations/{conv_id}")

    assert response.status_code == 204
    session.expire_all()
    assert session.get(Conversation, conv_id) is None
    assert session.exec(select(AgentRunLog).where(AgentRunLog.conversation_id == conv_id)).all() == []
    assert session.exec(select(AgentModelCall)).all() == []
    # The eval row keeps its search signals, unlinked from the deleted thread.
    assert session.get(PipelineEvalLog, eval_id).conversation_id is None
