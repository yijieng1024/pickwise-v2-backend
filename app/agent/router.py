from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.users.auth import get_current_user
from app.users.models import User

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentChatRequest(BaseModel):
    message: str


class AgentChatResponse(BaseModel):
    response: str


@router.post("/chat")
async def agent_chat(
    body: AgentChatRequest,
    current_user: User = Depends(get_current_user),
) -> AgentChatResponse:
    from app.agent.graph import run_agent

    try:
        response = await run_agent(body.message)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Agent unavailable. Ensure the MCP server is running on "
                f"{settings.mcp_server_url}. Error: {e}"
            ),
        )
    return AgentChatResponse(response=response)
