"""
Conversation service — CRUD for conversation threads.

The CRS chat pipeline (intent detection, retrieve/rerank/relax/gate
orchestration, LLM explanation) has been absorbed into the
`search_laptops` agent tool (see `app/agent/tools/search_laptops.py`) and
`app/agent/graph.py`/`router.py`, per
`project notes/CRS_Agent_Consolidation_Spec.md`. This module now only owns
conversation-thread lifecycle management, reused by `POST /agent/chat`.
"""
import uuid

from fastapi import HTTPException
from sqlmodel import Session, col, delete, select, update

from app.agent.monitoring_models import AgentModelCall, AgentRunLog
from app.rag.models import (
    Conversation,
    ConversationLaptop,
    Message,
    PipelineEvalLog,
)
from app.users.models import User


def _generate_title(message: str) -> str:
    """Truncate the first message to produce a readable conversation title."""
    clean = message.strip().replace("\n", " ")
    return clean[:60] + ("…" if len(clean) > 60 else "")


def create_conversation(user: User, session: Session) -> Conversation:
    conv = Conversation(
        user_id=user.id,
        title="New conversation",
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


def list_conversations(user: User, session: Session) -> list[Conversation]:
    return list(session.exec(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())  # type: ignore[arg-type]
    ).all())


def get_conversation(conversation_id: uuid.UUID, user: User, session: Session) -> Conversation:
    conv = session.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conv


def delete_conversation(conversation_id: uuid.UUID, user: User, session: Session) -> None:
    conv = session.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    # Every FK to conversations is NO ACTION, so each referencing row has to go
    # (or let go) first, or the delete is an IntegrityError 500. The run logs
    # hold the user's message and the reply verbatim, so they are deleted with
    # the conversation rather than kept: a deleted chat must not survive in the
    # monitoring tables. Chatbot Monitoring's history loses those runs. The
    # eval logs hold only search signals and keep their row, unlinked.
    run_ids = select(AgentRunLog.id).where(AgentRunLog.conversation_id == conversation_id)
    session.exec(delete(AgentModelCall).where(col(AgentModelCall.run_id).in_(run_ids)))
    session.exec(delete(AgentRunLog).where(AgentRunLog.conversation_id == conversation_id))
    session.exec(
        update(PipelineEvalLog)
        .where(PipelineEvalLog.conversation_id == conversation_id)
        .values(conversation_id=None)
    )
    session.exec(delete(Message).where(Message.conversation_id == conversation_id))
    session.exec(delete(ConversationLaptop).where(ConversationLaptop.conversation_id == conversation_id))

    session.delete(conv)
    session.commit()
