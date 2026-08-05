import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AgentRunLog(SQLModel, table=True):
    """One row per Pico agent turn (both /agent/chat and /agent/chat/stream).

    Written asynchronously via app.agent.monitoring_service.log_agent_run —
    never on the request path. Text fields are pre-truncated before this
    model is ever constructed; see monitoring_service._preview.
    """

    __tablename__ = "agent_run_logs"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)

    # Text, not the VARCHAR a bare `str` would declare — these hold truncated
    # but still long message bodies, and TEXT is what the live columns are.
    user_message: str = Field(sa_column=Column(Text, nullable=False))
    reply_text: str = Field(sa_column=Column(Text, nullable=False))

    status: str = Field(index=True, description="'success' or 'error'")
    error_message: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    latency_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

    # Each entry: {name, args_preview, output_preview, duration_ms, error}
    # NOT NULL: default_factory always supplies a list, and the live column is
    # NOT NULL — leaving it nullable here made `alembic check` propose a
    # spurious DROP NOT NULL.
    tool_calls: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class AgentRunLogSummary(SQLModel):
    """List-view schema — omits the heavy fields (reply text, full tool
    payloads) to keep the paginated response light."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    user_message: str
    status: str
    latency_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    tool_names: List[str]
    tool_call_count: int
    created_at: datetime


class AgentRunLogDetail(SQLModel):
    """Single-run detail schema — every field, full tool_calls."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    user_message: str
    reply_text: str
    status: str
    error_message: Optional[str] = None
    latency_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    tool_calls: List[Dict[str, Any]]
    created_at: datetime
