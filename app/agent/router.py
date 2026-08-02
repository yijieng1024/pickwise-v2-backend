import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.monitoring_service import MonitoringCallbackHandler, log_agent_run
from app.rag import service
from app.rag.models import Conversation, ConversationLaptop, Message, MessageRole
from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.logger import get_logger
from app.users.auth import get_current_user
from app.users.models import User

logger = get_logger(__name__)

router = APIRouter(prefix="/agent", tags=["Agent"])

# Shown to the end user on any agent failure — the real exception (which can
# include raw provider error payloads, e.g. Gemini rate-limit JSON) goes to
# AgentRunLog.error_message and the server log, never to the client.
_USER_FACING_ERROR = "Pico is unavailable right now. Please try again in a moment."


class AgentChatRequest(BaseModel):
    message: str
    conversation_id: Optional[uuid.UUID] = None


class AgentLaptopCard(BaseModel):
    laptop_id: uuid.UUID
    product_name: str
    price_rm: Optional[float] = None
    pick_score: Optional[int] = None
    similarity_score: Optional[float] = None
    # First catalog photo, for the card thumbnail. Looked up from the Laptop
    # row here rather than added to the search tool's payload, so image URLs
    # never enter the LLM context.
    image_url: Optional[str] = None


class AgentChatResponse(BaseModel):
    response: str
    conversation_id: uuid.UUID
    # The conversation's current laptop shortlist — fresh search results when
    # search_laptops ran this turn, otherwise the persisted pool, so the
    # frontend can keep rendering score badges on follow-up turns too.
    laptops: list[AgentLaptopCard] = []


def _setup_turn(
    body: AgentChatRequest, current_user: User, session: Session
) -> tuple[Conversation, list[Message], list[ConversationLaptop]]:
    """Resolve/create the conversation, load history + shortlist pool, and
    persist the user's message. Shared by the plain and streaming endpoints."""
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
    return conv, history, conv_laptops


def _persist_assistant_turn(
    conv: Conversation,
    reply_text: str,
    tool_results: Optional[list[dict]],
    conv_laptops: list[ConversationLaptop],
    session: Session,
) -> list[AgentLaptopCard]:
    """Persist the assistant message + shortlist pool and return the laptop
    cards for the response. Shared by the plain and streaming endpoints."""
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
        cards = []
        for r in tool_results:
            laptop = session.get(Laptop, r["laptop_id"])
            cards.append(AgentLaptopCard(
                laptop_id=r["laptop_id"],
                product_name=r["product_name"],
                price_rm=r.get("price_rm"),
                pick_score=r.get("pick_score"),
                similarity_score=r.get("similarity_score"),
                image_url=laptop.image_urls[0] if laptop and laptop.image_urls else None,
            ))
        return cards

    # No new search this turn — return the persisted shortlist pool so the
    # frontend keeps its cards on follow-up questions.
    pool_rows = session.exec(
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
    ) for cl, laptop in pool_rows]


@router.post("/chat")
async def agent_chat(
    body: AgentChatRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentChatResponse:
    from app.agent.graph import run_agent

    conv, history, conv_laptops = _setup_turn(body, current_user, session)
    handler = MonitoringCallbackHandler()
    turn_started = time.monotonic()

    try:
        reply_text, tool_results = await run_agent(
            body.message, history, conv_laptops, session, user_id=current_user.id, handler=handler,
        )
    except Exception as e:
        # Raising here bypasses the response FastAPI would have attached
        # `background_tasks` to, so log synchronously — this is the rare
        # agent-failure path, not the hot path BackgroundTasks exists for.
        log_agent_run(
            conv.id, current_user.id, body.message, "", "error", str(e),
            round((time.monotonic() - turn_started) * 1000), handler,
        )
        raise HTTPException(
            status_code=503,
            detail=_USER_FACING_ERROR,
        )

    laptop_cards = _persist_assistant_turn(conv, reply_text, tool_results, conv_laptops, session)

    background_tasks.add_task(
        log_agent_run,
        conv.id, current_user.id, body.message, reply_text, "success", None,
        round((time.monotonic() - turn_started) * 1000), handler,
    )

    return AgentChatResponse(
        response=reply_text,
        conversation_id=conv.id,
        laptops=laptop_cards,
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/chat/stream")
async def agent_chat_stream(
    body: AgentChatRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE twin of /chat: streams the reply token-by-token.

    Events (JSON per `data:` line):
      meta       {conversation_id}            — first event, always
      token      {text}                       — append to the assistant bubble
      thinking   {text}                       — append to the thinking-flow
                                                panel (model reasoning delta)
      turn_reset {}                           — discard the current bubble text
                                                (internal tool-call turn)
      tool       {name}                       — tool started (activity chip)
      done       {conversation_id, laptops}   — terminal on success; laptops
                                                matches AgentChatResponse.laptops
      error      {detail}                     — terminal on failure (the HTTP
                                                status is already 200 by then)
    """
    from app.agent.graph import stream_agent

    conv, history, conv_laptops = _setup_turn(body, current_user, session)
    handler = MonitoringCallbackHandler()
    turn_started = time.monotonic()

    async def sse_gen():
        yield _sse({"type": "meta", "conversation_id": str(conv.id)})

        reply_text = ""
        tool_results: Optional[list[dict]] = None
        try:
            async for ev in stream_agent(
                body.message, history, conv_laptops, session, user_id=current_user.id, handler=handler,
            ):
                if ev["type"] == "final":
                    reply_text = ev["reply"]
                    tool_results = ev["tool_results"]
                else:
                    yield _sse(ev)
        except Exception as e:
            logger.exception("agent stream failed for conversation %s", conv.id)
            background_tasks.add_task(
                log_agent_run,
                conv.id, current_user.id, body.message, reply_text, "error", str(e),
                round((time.monotonic() - turn_started) * 1000), handler,
            )
            yield _sse({"type": "error", "detail": _USER_FACING_ERROR})
            return

        laptop_cards = _persist_assistant_turn(
            conv, reply_text, tool_results, conv_laptops, session
        )
        background_tasks.add_task(
            log_agent_run,
            conv.id, current_user.id, body.message, reply_text, "success", None,
            round((time.monotonic() - turn_started) * 1000), handler,
        )
        yield _sse({
            "type": "done",
            "conversation_id": str(conv.id),
            "laptops": [c.model_dump(mode="json") for c in laptop_cards],
        })

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={
            # Keep proxies (Render's included) from buffering tokens into
            # one lump; SSE responses must never be cached.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
        # BackgroundTasks added inside sse_gen() (both on success and the
        # internal except branch) land in this same object — FastAPI only
        # auto-attaches an injected BackgroundTasks to responses it builds
        # itself, not to a Response subclass returned directly, so it must
        # be passed explicitly here.
        background=background_tasks,
    )
