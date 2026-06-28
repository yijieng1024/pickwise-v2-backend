"""
Conversation service — orchestrates the full CRS pipeline per chat turn.

Flow per message:
  1. Save user message to DB
  2. Detect intent (Gemini): "related" (continue context) or "new_search"
  3a. new_search → Modules 1-4 → PickScore → Gemini LLM explanation
  3b. related    → answer follow-up using existing conversation_laptops
  4. Save assistant message + update conversation_laptops if new search
  5. Log to pipeline_eval_logs
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.config import settings
from app.conversations.evaluation import log_pipeline_result
from app.conversations.gating import relevance_gate
from app.conversations.models import (
    Conversation,
    ConversationLaptop,
    Message,
    MessageRole,
)
from app.conversations.relaxation import needs_relaxation, relax_and_retry
from app.conversations.reranker import UserConstraints, rerank
from app.conversations.retrieval import retrieve_candidates
from app.conversations.schemas import ChatResponse, RecommendedLaptopInChat
from app.laptops.laptop_models import Laptop, LaptopEmbedding
from app.laptops.brand_model import LaptopBrand
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.pickscore.engine import calculate_pick_score
from app.recommendation.schemas import _LLMOutput, _PerLaptopExplanation
from app.users.models import LaptopUserPreference, User


# ── LLM helpers ───────────────────────────────────────────────────────────────

_TECH_SAVVY_INSTRUCTION = {
    "Very tech-savvy": "Use technical terminology freely — benchmark scores, TDP, clock speeds, VRAM.",
    "Somewhat tech-savvy": "Balance technical terms with brief plain-English context.",
    "Not very tech-savvy": "Use plain, everyday language. Focus on real-world outcomes. Avoid jargon.",
}

_INTENT_PROMPT = (
    "You are classifying user intent in a laptop recommendation conversation.\n"
    "Conversation so far:\n{history}\n\n"
    "New message: \"{message}\"\n\n"
    "Is this message asking about the laptops already recommended (follow-up), "
    "or is it a completely new laptop search request?\n"
    "Reply with exactly one word: related  OR  new_search"
)

_NEW_SEARCH_SYSTEM = (
    "You are PickWise, an expert laptop recommendation assistant. "
    "Explain why each shortlisted laptop fits or doesn't fit the user. "
    "Communication style: {tech_instruction}"
)

_NEW_SEARCH_HUMAN = (
    'User message: "{message}"\n\n'
    "User profile:\n{pref_context}\n\n"
    "Shortlisted laptops (ranked by PickScore):\n\n{laptops_block}\n\n"
    "Instructions:\n"
    "- Write a 2-3 sentence explanation per laptop.\n"
    "- Write a 3-4 sentence overall summary recommending the best pick.\n"
    "- Use the exact laptop_id UUIDs shown above.\n"
    "- Match your language to the communication style above."
)

_FOLLOWUP_SYSTEM = (
    "You are PickWise, a laptop recommendation assistant in an ongoing conversation. "
    "Answer the user's follow-up question using only the laptops listed below. "
    "Communication style: {tech_instruction}"
)

_FOLLOWUP_HUMAN = (
    "Laptops in this conversation:\n{laptops_block}\n\n"
    "Conversation history:\n{history}\n\n"
    "User follow-up: \"{message}\"\n\n"
    "Answer directly and concisely. Reference laptops by name."
)


def _get_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        temperature=0.3,
        google_api_key=settings.gemini_api_key,
    )


def _format_history(messages: list[Message], limit: int = 6) -> str:
    recent = messages[-limit:]
    return "\n".join(f"{m.role.value.capitalize()}: {m.content}" for m in recent)


def _laptop_block_for_followup(session: Session, conv_laptops: list[ConversationLaptop]) -> str:
    lines = []
    for cl in conv_laptops:
        laptop = session.get(Laptop, cl.laptop_id)
        if not laptop:
            continue
        brand = session.exec(
            select(LaptopBrand).where(LaptopBrand.id == laptop.brand_id)
        ).first()
        brand_name = brand.name if brand else "Unknown"
        lines.append(
            f"- {brand_name} {laptop.product_name} | RM {laptop.price_rm:.0f} | "
            f"{laptop.weight_kg}kg | {laptop.ram_gb}GB RAM | {laptop.processor_model} | "
            f"PickScore: {cl.pick_score}"
        )
    return "\n".join(lines)


# ── Title generation ──────────────────────────────────────────────────────────

def _generate_title(message: str) -> str:
    """Truncate the first message to produce a readable conversation title."""
    clean = message.strip().replace("\n", " ")
    return clean[:60] + ("…" if len(clean) > 60 else "")


# ── Intent detection ──────────────────────────────────────────────────────────

def _detect_intent(message: str, history: str) -> str:
    """Returns 'related' or 'new_search'."""
    llm = _get_llm()
    chain = ChatPromptTemplate.from_messages([
        ("human", _INTENT_PROMPT),
    ]) | llm
    result = chain.invoke({"history": history, "message": message})
    raw = result.content.strip().lower()
    return "related" if "related" in raw else "new_search"


# ── New-search pipeline ───────────────────────────────────────────────────────

def _build_laptop_spec_block(laptop, brand_name: str, pick_resp, similarity: float, final_score: float) -> str:
    breakdown_str = " | ".join(f"{f.factor}={f.raw_score:.0f}" for f in pick_resp.breakdown)
    return (
        f"laptop_id: {laptop.id}\n"
        f"Name: {brand_name} {laptop.product_name}\n"
        f"Price: RM {laptop.price_rm:.0f} | Weight: {laptop.weight_kg}kg\n"
        f"PickScore: {pick_resp.score}/100 | Similarity: {similarity:.3f} | Final: {final_score:.3f}\n"
        f"CPU: {laptop.processor_model} | GPU: {laptop.gpu_model}\n"
        f"RAM: {laptop.ram_gb}GB | Storage: {laptop.ssd_gb}GB {laptop.storage_type or ''}\n"
        f"Score breakdown: {breakdown_str}"
    )


def _run_new_search(
    message: str,
    user_pref: LaptopUserPreference,
    constraints: UserConstraints,
    session: Session,
    top_k: int = 3,
) -> tuple[ChatResponse, Optional[object], Optional[object]]:
    """
    Full CRS pipeline: retrieve → rerank → relax → gate → PickScore → LLM.
    Returns (ChatResponse, gate_result, relaxation_result).
    """
    # Modules 1 → 2
    candidates = retrieve_candidates(message, session, budget_max=constraints.budget)
    ranked = rerank(candidates, constraints)

    # Module 3
    relaxation = None
    if needs_relaxation(ranked):
        relaxation = relax_and_retry(message, constraints, session)
        if relaxation:
            ranked = relaxation.candidates
        else:
            ranked = []

    # Module 4
    gate = relevance_gate(ranked)

    if gate.status == "gated":
        return (
            ChatResponse(
                content=gate.message or "I couldn't find a good match. Could you clarify your needs?",
                gate_status="gated",
                relaxation_notice=relaxation.user_message if relaxation else None,
                conversation_id=uuid.uuid4(),  # placeholder — caller replaces this
            ),
            gate,
            relaxation,
        )

    # PickScore the top_k
    ranges = get_laptop_ranges(session)
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]

    top = gate.candidates[:top_k]
    scored = []
    for rc in top:
        laptop = rc._laptop  # type: ignore[attr-defined]
        brand_name = rc._brand_name  # type: ignore[attr-defined]
        product = laptop_to_scorable(laptop, brand_name)
        pick_resp = calculate_pick_score(product, user_pref, ranges, cpu_benchmarks, gpu_benchmarks)
        scored.append((rc, laptop, brand_name, pick_resp))

    scored.sort(key=lambda x: x[3].score, reverse=True)  # type: ignore[attr-defined]

    # LLM explanation
    tech_instruction = _TECH_SAVVY_INSTRUCTION.get(
        user_pref.tech_savviness or "", _TECH_SAVVY_INSTRUCTION["Somewhat tech-savvy"]
    )
    pref_context = (
        f"Budget: RM {user_pref.budget or 'not set'} | "
        f"Purpose: {', '.join(user_pref.purpose or ['not specified'])} | "
        f"Portability: {user_pref.portability or 'neutral'}"
    )
    laptops_block = "\n\n".join(
        _build_laptop_spec_block(laptop, brand_name, pick_resp, rc.similarity_score, rc.final_score)
        for rc, laptop, brand_name, pick_resp in scored
    )

    llm = _get_llm()
    structured_llm = llm.with_structured_output(_LLMOutput)
    chain = ChatPromptTemplate.from_messages([
        ("system", _NEW_SEARCH_SYSTEM),
        ("human", _NEW_SEARCH_HUMAN),
    ]) | structured_llm
    llm_out: _LLMOutput = chain.invoke({  # type: ignore[assignment]
        "tech_instruction": tech_instruction,
        "message": message,
        "pref_context": pref_context,
        "laptops_block": laptops_block,
    })

    explanation_map = {e.laptop_id: e.explanation for e in llm_out.explanations}
    recommendations = [
        RecommendedLaptopInChat(
            laptop_id=laptop.id,
            product_name=f"{brand_name} {laptop.product_name}",
            price_rm=laptop.price_rm,
            pick_score=pick_resp.score,  # type: ignore[attr-defined]
            similarity_score=rc.similarity_score,
            final_score=rc.final_score,
            breakdown=pick_resp.breakdown,  # type: ignore[attr-defined]
            explanation=explanation_map.get(str(laptop.id), ""),
        )
        for rc, laptop, brand_name, pick_resp in scored
    ]

    content = llm_out.summary
    if relaxation:
        content = f"{relaxation.user_message}\n\n{content}"

    return (
        ChatResponse(
            content=content,
            recommendations=recommendations,
            gate_status="pass",
            relaxation_notice=relaxation.user_message if relaxation else None,
            conversation_id=uuid.uuid4(),  # placeholder — caller replaces this
        ),
        gate,
        relaxation,
    )


# ── Follow-up handler ─────────────────────────────────────────────────────────

def _run_followup(
    message: str,
    history: str,
    conv_laptops: list[ConversationLaptop],
    user_pref: LaptopUserPreference,
    session: Session,
    conversation_id: uuid.UUID,
) -> ChatResponse:
    tech_instruction = _TECH_SAVVY_INSTRUCTION.get(
        user_pref.tech_savviness or "", _TECH_SAVVY_INSTRUCTION["Somewhat tech-savvy"]
    )
    laptops_block = _laptop_block_for_followup(session, conv_laptops)

    llm = _get_llm()
    chain = ChatPromptTemplate.from_messages([
        ("system", _FOLLOWUP_SYSTEM),
        ("human", _FOLLOWUP_HUMAN),
    ]) | llm
    result = chain.invoke({
        "tech_instruction": tech_instruction,
        "laptops_block": laptops_block,
        "history": history,
        "message": message,
    })

    return ChatResponse(
        content=result.content,
        recommendations=[],
        gate_status="pass",
        conversation_id=conversation_id,
    )


# ── Public API ────────────────────────────────────────────────────────────────

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

    # Delete children first (FK constraints)
    for msg in session.exec(select(Message).where(Message.conversation_id == conversation_id)).all():
        session.delete(msg)
    for cl in session.exec(select(ConversationLaptop).where(ConversationLaptop.conversation_id == conversation_id)).all():
        session.delete(cl)

    session.delete(conv)
    session.commit()


def handle_chat(
    conversation_id: uuid.UUID,
    message: str,
    user: User,
    session: Session,
) -> ChatResponse:
    # Verify ownership
    conv = session.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    # Require preference profile
    user_pref = session.exec(
        select(LaptopUserPreference).where(LaptopUserPreference.user_id == user.id)
    ).first()
    if not user_pref:
        raise HTTPException(
            status_code=400,
            detail="Please complete your preference profile before chatting.",
        )

    # Load conversation state
    messages = list(session.exec(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())  # type: ignore[arg-type]
    ).all())
    conv_laptops = list(session.exec(
        select(ConversationLaptop)
        .where(ConversationLaptop.conversation_id == conversation_id)
    ).all())

    # Persist user message
    user_msg = Message(
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=message,
    )
    session.add(user_msg)

    # Auto-title on first message
    if not messages:
        conv.title = _generate_title(message)

    session.commit()

    # Build constraints from preference profile
    constraints = UserConstraints.from_preference(user_pref)

    # Determine intent
    history = _format_history(messages)
    is_followup = bool(conv_laptops) and _detect_intent(message, history) == "related"

    gate_result = None
    relaxation_result = None

    if is_followup:
        response = _run_followup(message, history, conv_laptops, user_pref, session, conversation_id)
    else:
        response, gate_result, relaxation_result = _run_new_search(
            message, user_pref, constraints, session
        )
        response.conversation_id = conversation_id

        # Replace conversation_laptops pool on new search
        if response.gate_status == "pass" and response.recommendations:
            for old in conv_laptops:
                session.delete(old)
            for rec in response.recommendations:
                session.add(ConversationLaptop(
                    conversation_id=conversation_id,
                    laptop_id=rec.laptop_id,
                    pick_score=rec.pick_score,
                    similarity_score=rec.similarity_score,
                ))

    # Persist assistant reply
    session.add(Message(
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT,
        content=response.content,
    ))

    # Bump conversation timestamp
    conv.updated_at = datetime.now(timezone.utc)
    session.commit()

    # Log pipeline metrics (fire-and-forget, never raises)
    if gate_result is not None:
        log_pipeline_result(
            gate=gate_result,
            query=message,
            relaxation=relaxation_result,
            user_id=user.id,
            conversation_id=conversation_id,
            session=session,
        )

    return response
