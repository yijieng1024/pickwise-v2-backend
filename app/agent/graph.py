import json
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from sqlmodel import Session

from app.agent.tools import ALL_TOOLS
from app.config import settings
from app.rag.models import ConversationLaptop, Message, MessageRole
from app.laptops.laptop_models import Laptop

# How many prior turns (user + assistant messages combined) to replay into
# the agent's context window each call. Reconstructed from the `messages`
# table each turn instead of a LangGraph checkpointer — see
# CRS_Agent_Consolidation_Spec.md and the migration plan for why.
_HISTORY_WINDOW = 12

_SYSTEM_PROMPT = (
    "You are PickWise Agent, an expert laptop buying consultant. You have "
    "access to four tools:\n"
    "1. search_laptops — search the catalog using semantic search corrected "
    "against explicit constraints (budget, brand, purpose). Use this whenever "
    "the user describes what they want in a new or changed way.\n"
    "2. calculate_custom_apple_price — calculate the exact price of an Apple "
    "laptop with selected upgrade options (e.g. more RAM, larger SSD).\n"
    "3. get_review_evidence — retrieve real YouTube reviewer opinions for a "
    "laptop, ranked by relevance to the user's stated priorities.\n"
    "4. search_malaysian_market_price — get direct Shopee/Lazada search links "
    "for a laptop so the user can check live listings (not yet a live price "
    "lookup).\n\n"
    "IMPORTANT — handling search_laptops results:\n"
    "- If confidence is \"high\", present the results and explain why they fit.\n"
    "- If confidence is \"low\", you MUST NOT simply say no laptops were found "
    "and stop. The tool intentionally withholds a forced, poorly-matching "
    "recommendation — it is now your job to keep the conversation productive. "
    "Use the bottleneck and message fields to gently explain why nothing "
    "matched well and guide the user toward a specific way forward: if "
    "bottleneck is \"budget\", suggest a modest budget increase; if "
    "\"weight_limit\", ask whether they'd accept a slightly heavier machine "
    "or whether portability is non-negotiable; if \"general\", ask which "
    "requirement (budget, performance, portability, or brand) they're most "
    "flexible on. If relaxation_notice is set, mention that a constraint was "
    "already loosened to find the results shown.\n\n"
    "You already have the conversation history and, if present, the current "
    "shortlist of laptops. For follow-up questions about laptops already "
    "shown, answer from that context instead of calling search_laptops again. "
    "Be concise. Always show prices in RM. Include the model code when "
    "referencing a specific Apple laptop so the user (or you) can use it with "
    "calculate_custom_apple_price."
)

_SEARCH_LAPTOPS_TOOL_NAME = "search_laptops"


def _pool_block(conv_laptops: list[ConversationLaptop], session: Session) -> Optional[str]:
    if not conv_laptops:
        return None

    lines = []
    for cl in conv_laptops:
        laptop = session.get(Laptop, cl.laptop_id)
        if not laptop:
            continue
        lines.append(
            f"- laptop_id={laptop.id} | {laptop.product_name} | RM {laptop.price_rm:.0f} | "
            f"{laptop.processor_model} | {laptop.gpu_model} | {laptop.ram_gb}GB RAM | "
            f"{laptop.weight_kg}kg"
        )
    if not lines:
        return None
    return "Current shortlist in this conversation:\n" + "\n".join(lines)


def _build_message_history(history: list[Message]) -> list:
    langchain_messages = []
    for m in history[-_HISTORY_WINDOW:]:
        cls = HumanMessage if m.role == MessageRole.USER else AIMessage
        langchain_messages.append(cls(content=m.content))
    return langchain_messages


def _extract_search_results(messages: list) -> Optional[list[dict]]:
    """
    Scan the agent's message trace for the most recent search_laptops
    ToolMessage from this turn. Returns its `results` list when confidence
    was "high", else None (either not called, or gated).
    """
    for m in reversed(messages):
        if getattr(m, "name", None) != _SEARCH_LAPTOPS_TOOL_NAME:
            continue
        content = m.content
        try:
            payload = content if isinstance(content, dict) else json.loads(content)
        except (TypeError, ValueError):
            return None
        if payload.get("confidence") == "high":
            return payload.get("results") or None
        return None
    return None


async def run_agent(
    message: str,
    history: list[Message],
    conv_laptops: list[ConversationLaptop],
    session: Session,
) -> tuple[str, Optional[list[dict]]]:
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash",
        temperature=0.3,
        google_api_key=settings.gemini_api_key,
    )

    langchain_messages = [SystemMessage(content=_SYSTEM_PROMPT)]

    pool_block = _pool_block(conv_laptops, session)
    if pool_block:
        langchain_messages.append(SystemMessage(content=pool_block))

    langchain_messages.extend(_build_message_history(history))
    langchain_messages.append(HumanMessage(content=message))

    agent = create_react_agent(llm, ALL_TOOLS)
    result = await agent.ainvoke({"messages": langchain_messages})

    reply_text = result["messages"][-1].content
    tool_results = _extract_search_results(result["messages"])

    return reply_text, tool_results
