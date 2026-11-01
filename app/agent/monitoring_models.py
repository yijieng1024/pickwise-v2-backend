import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, Text, UniqueConstraint
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

    # The model that wrote the reply (last completed LLM call of the turn), its
    # position in the chain (0 = primary, 1..n = backup index), and whether a
    # backup answered at least one step. NULL model/position = no LLM call
    # completed (the turn errored first), or a row from before the columns.
    # fallback_triggered on rows before agent_model_calls means only that a
    # backup was started.
    model: Optional[str] = Field(default=None, max_length=100)
    model_position: Optional[int] = None
    fallback_triggered: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})

    # Each entry: {name, args_preview, output_preview, duration_ms, error}
    # NOT NULL: default_factory always supplies a list, and the live column is
    # NOT NULL — leaving it nullable here made `alembic check` propose a
    # spurious DROP NOT NULL.
    tool_calls: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class AgentModelCall(SQLModel, table=True):
    """One row per model call of an agent turn, failed calls included. Written
    with its AgentRunLog by log_agent_run."""

    __tablename__ = "agent_model_calls"  # type: ignore
    # Also the index for reading a run's calls.
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_model_call_order"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(foreign_key="agent_run_logs.id")
    # The ReAct step, from 1. A failed call and the fallback that answered for
    # it share a step, so `seq` is what orders them: 1..n across the run.
    step: int
    seq: int
    model: str = Field(max_length=100)
    chain_position: int  # 0 = primary, 1..n = backup index
    is_fallback: bool
    status: str = Field(max_length=20, description="'answered' or 'error'")
    error_type: Optional[str] = Field(default=None, max_length=100)
    error_message: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    started_at: datetime  # naive UTC, like llm_usage_logs.created_at
    # Includes the wait for the model's rate limiter, up to 5 s.
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # Names of the tools this call asked for.
    tool_calls: List[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))


class AgentModelCallRead(SQLModel):
    step: int
    seq: int
    model: str
    chain_position: int
    is_fallback: bool
    status: str
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    started_at: datetime
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    tool_calls: List[str]


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
    model: Optional[str] = None
    # 0 = primary answered, 1..n = that backup (null: no LLM call completed)
    model_position: Optional[int] = None
    fallback_triggered: bool = False
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
    model: Optional[str] = None
    # 0 = primary answered, 1..n = that backup (null: no LLM call completed)
    model_position: Optional[int] = None
    fallback_triggered: bool = False
    tool_calls: List[Dict[str, Any]]
    # In call order. Empty for runs from before the table.
    model_calls: List[AgentModelCallRead] = []
    created_at: datetime
