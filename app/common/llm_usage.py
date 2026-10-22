"""
Token usage for every LLM call in the app, in one table.

`agent_run_logs` already records tokens per agent turn, but the processor,
recommendation and review pipelines called Gemini with no record at all, so
"how many tokens did we spend today" had no answer. `llm_usage_logs` is that
answer: one row per LLM call (one per agent *turn* — the agent already sums
its own steps in MonitoringCallbackHandler, and `log_agent_run` writes the
row).

Wiring is a callback on the model constructor — `callbacks=[TokenUsageHandler(...)]`
— so call sites change one argument and their logic not at all. Writes are
best-effort, exactly like monitoring_service: a failed usage write must never
fail the LLM call it is describing.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from sqlmodel import Field, Session, SQLModel

from app.database import engine
from app.logger import get_logger

logger = get_logger(__name__)


class LLMFeature:
    """Values for `llm_usage_logs.feature`. Plain strings in a VARCHAR, so a
    new feature needs no migration — constants only so a typo at a call site
    cannot split one feature's usage across two spellings."""

    AGENT = "agent"
    PROCESSOR_EXTRACT = "processor_extract"
    PROCESSOR_CATEGORIZE = "processor_categorize"
    RECOMMENDATION = "recommendation"
    REVIEW_CHUNK = "review_chunk"


class LLMUsageLog(SQLModel, table=True):
    __tablename__ = "llm_usage_logs"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    feature: str = Field(index=True, max_length=50)
    model: str = Field(max_length=100)
    input_tokens: int = 0
    output_tokens: int = 0
    # Where `model` sits in the use's chain: 0 = primary, 1..n = backups in
    # order (app/common/llm.py). `is_fallback` is position > 0, kept as its own
    # column so "how often did we fall back" needs no arithmetic.
    # `model` is always the model that actually answered.
    is_fallback: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    model_position: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # Null for calls no user triggered directly (admin batch jobs).
    user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    # Naive UTC, matching agent_run_logs.created_at's live column type.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True
    )


def log_llm_usage(
    feature: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    user_id: Optional[uuid.UUID] = None,
    model_position: int = 0,
) -> None:
    """Persist one usage row on its own short session. Never raises."""
    try:
        with Session(engine) as session:
            session.add(LLMUsageLog(
                feature=feature, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                user_id=user_id,
                is_fallback=model_position > 0, model_position=model_position,
            ))
            session.commit()
    except Exception as e:
        # One line, no traceback: this runs once per LLM call, so a database
        # outage (CI with an unreachable DB) would otherwise bury every log in
        # identical stacks. Monitoring only -- the call itself is unaffected.
        logger.warning(
            "failed to write llm_usage_log (%s: %s); call unaffected",
            type(e).__name__, str(e).strip().split("\n")[0][:200],
        )


def usage_from_response(response: Any) -> tuple[int, int]:
    """Sum (input, output) tokens across an LLMResult's generations.
    Zero when the provider returned no usage_metadata."""
    tokens_in = tokens_out = 0
    for gen_list in response.generations:
        for gen in gen_list:
            usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
            if usage:
                tokens_in += usage.get("input_tokens") or 0
                tokens_out += usage.get("output_tokens") or 0
    return tokens_in, tokens_out


class TokenUsageHandler(BaseCallbackHandler):
    """Writes one llm_usage_logs row per completed LLM call. Construct per
    model: `ChatGoogleGenerativeAI(..., callbacks=[TokenUsageHandler(feature, model)])`.
    Errored calls fire on_llm_error instead of on_llm_end and are not logged —
    a failed call reports no usage to log."""

    def __init__(
        self, feature: str, model: str, user_id: Optional[uuid.UUID] = None, model_position: int = 0,
    ) -> None:
        self.feature = feature
        self.model = model
        self.user_id = user_id
        self.model_position = model_position

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        tokens_in, tokens_out = usage_from_response(response)
        if tokens_in or tokens_out:
            log_llm_usage(self.feature, self.model, tokens_in, tokens_out, self.user_id, self.model_position)
