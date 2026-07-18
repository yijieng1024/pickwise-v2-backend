import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

# Safe one-way import: agent.router consumes rag.service/models, never this
# router, so borrowing its card schema keeps the two endpoints' shapes locked.
from app.agent.router import AgentLaptopCard
from app.laptops.laptop_models import Laptop
from app.rag import service
from app.rag.models import ConversationLaptop, ConversationRead, ConversationSummary
from app.database import get_session
from app.users.auth import get_current_user
from app.users.models import User

router = APIRouter(prefix="/conversations", tags=["Conversations"])


@router.post("/", response_model=ConversationSummary, status_code=status.HTTP_201_CREATED)
def create_conversation(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return service.create_conversation(current_user, session)


@router.get("/", response_model=List[ConversationSummary])
def list_conversations(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return service.list_conversations(current_user, session)


@router.get("/{conversation_id}", response_model=ConversationRead)
def get_conversation(
    conversation_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return service.get_conversation(conversation_id, current_user, session)


class ConversationRename(BaseModel):
    title: str


@router.patch("/{conversation_id}", response_model=ConversationSummary)
def rename_conversation(
    conversation_id: uuid.UUID,
    body: ConversationRename,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Rename a conversation. The title is trimmed and capped at 60 chars,
    matching the auto-generated titles."""
    conv = service.get_conversation(conversation_id, current_user, session)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    conv.title = title[:60]
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


@router.get("/{conversation_id}/laptops", response_model=List[AgentLaptopCard])
def get_conversation_laptops(
    conversation_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """The conversation's persisted laptop shortlist pool — the same cards
    (and order) the agent's `done` event carries, so the frontend can restore
    the shortlist rail when reopening a past conversation."""
    conv = service.get_conversation(conversation_id, current_user, session)

    rows = session.exec(
        select(ConversationLaptop, Laptop)
        .join(Laptop, ConversationLaptop.laptop_id == Laptop.id)  # type: ignore[arg-type]
        .where(ConversationLaptop.conversation_id == conv.id)
        .order_by(ConversationLaptop.similarity_score.desc().nullslast())  # type: ignore[union-attr]
    ).all()
    return [AgentLaptopCard(
        laptop_id=cl.laptop_id,
        product_name=laptop.product_name,
        price_rm=laptop.price_rm,
        pick_score=cl.pick_score,
        similarity_score=cl.similarity_score,
        image_url=laptop.image_urls[0] if laptop.image_urls else None,
    ) for cl, laptop in rows]


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service.delete_conversation(conversation_id, current_user, session)
