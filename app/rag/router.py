import uuid
from typing import List

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.rag import service
from app.rag.models import ConversationRead, ConversationSummary
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


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service.delete_conversation(conversation_id, current_user, session)
