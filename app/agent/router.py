import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.rag import service
from app.rag.models import ConversationLaptop, Message, MessageRole
from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.users.auth import get_current_user
from app.users.models import User

router = APIRouter(prefix="/agent", tags=["Agent"])


class AgentChatRequest(BaseModel):
    message: str
    conversation_id: Optional[uuid.UUID] = None


class AgentLaptopCard(BaseModel):
    laptop_id: uuid.UUID
    product_name: str
    price_rm: Optional[float] = None
    pick_score: Optional[int] = None
    similarity_score: Optional[float] = None


class AgentChatResponse(BaseModel):
    response: str
    conversation_id: uuid.UUID
    # The conversation's current laptop shortlist — fresh search results when
    # search_laptops ran this turn, otherwise the persisted pool, so the
    # frontend can keep rendering score badges on follow-up turns too.
    laptops: list[AgentLaptopCard] = []


@router.post("/chat")
async def agent_chat(
    body: AgentChatRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentChatResponse:
    from app.agent.graph import run_agent

    if body.conversation_id:
        conv = service.get_conversation(body.conversation_id, current_user, session)
    else:
        conv = service.create_conversation(current_user, session)

    history = list(session.exec(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.asc())  # type: ignore[arg-type]
    ).all())
    conv_laptops = list(session.exec(
        select(ConversationLaptop)
        .where(ConversationLaptop.conversation_id == conv.id)
    ).all())

    if not history:
        conv.title = service._generate_title(body.message)

    session.add(Message(
        conversation_id=conv.id,
        role=MessageRole.USER,
        content=body.message,
    ))
    session.commit()

    try:
        reply_text, tool_results = await run_agent(body.message, history, conv_laptops, session)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Agent unavailable: {e}",
        )

    session.add(Message(
        conversation_id=conv.id,
        role=MessageRole.ASSISTANT,
        content=reply_text,
    ))

    if tool_results is not None:
        for old in conv_laptops:
            session.delete(old)
        for r in tool_results:
            session.add(ConversationLaptop(
                conversation_id=conv.id,
                laptop_id=r["laptop_id"],
                pick_score=r.get("pick_score"),
                similarity_score=r.get("similarity_score"),
            ))

    conv.updated_at = datetime.now(timezone.utc)
    session.commit()

    if tool_results is not None:
        laptop_cards = [AgentLaptopCard(
            laptop_id=r["laptop_id"],
            product_name=r["product_name"],
            price_rm=r.get("price_rm"),
            pick_score=r.get("pick_score"),
            similarity_score=r.get("similarity_score"),
        ) for r in tool_results]
    else:
        # No new search this turn — return the persisted shortlist pool so the
        # frontend keeps its cards on follow-up questions.
        pool_rows = session.exec(
            select(ConversationLaptop, Laptop)
            .join(Laptop, ConversationLaptop.laptop_id == Laptop.id)  # type: ignore[arg-type]
            .where(ConversationLaptop.conversation_id == conv.id)
            .order_by(ConversationLaptop.similarity_score.desc().nullslast())  # type: ignore[union-attr]
        ).all()
        laptop_cards = [AgentLaptopCard(
            laptop_id=cl.laptop_id,
            product_name=laptop.product_name,
            price_rm=laptop.price_rm,
            pick_score=cl.pick_score,
            similarity_score=cl.similarity_score,
        ) for cl, laptop in pool_rows]

    return AgentChatResponse(
        response=reply_text,
        conversation_id=conv.id,
        laptops=laptop_cards,
    )
