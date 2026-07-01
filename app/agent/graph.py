from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from app.agent.tools import calculate_custom_apple_price, get_review_evidence, search_laptops
from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are PickWise Agent, an expert laptop buying assistant. "
    "You have access to three tools:\n"
    "1. search_laptops — find laptops in the catalog by budget, brand, or minimum RAM\n"
    "2. calculate_custom_apple_price — calculate the exact price of an Apple laptop "
    "with selected upgrade options (e.g. more RAM, larger SSD)\n"
    "3. get_review_evidence — retrieve real YouTube reviewer opinions for a laptop, "
    "ranked by relevance to the user's stated priorities\n\n"
    "Be concise. Always show prices in RM. Include the model code when referencing "
    "a specific Apple laptop so the user can use it with calculate_custom_apple_price."
)

_TOOLS = [search_laptops, calculate_custom_apple_price, get_review_evidence]


async def run_agent(message: str) -> str:
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash",
        temperature=0.3,
        google_api_key=settings.gemini_api_key,
    )

    agent = create_react_agent(llm, _TOOLS)
    result = await agent.ainvoke({
        "messages": [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=message),
        ]
    })

    return result["messages"][-1].content
