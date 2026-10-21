"""
Best-effort telemetry capture for the Pico agent, written off the request
path via FastAPI BackgroundTasks (see app/agent/router.py). A write failure
here must never surface to the chat response — every DB-touching function
in this module swallows its own exceptions and just logs.

Tracing is done with a LangChain AsyncCallbackHandler (MonitoringCallbackHandler)
rather than by hand-instrumenting individual tools or splicing capture calls
into astream_events string-matching: a handler passed via
`config={"callbacks": [...]}` propagates automatically to every node of the
CompiledStateGraph that create_agent(...) returns, so the same handler class
covers both the streaming and non-streaming call sites with identical
fidelity, and app/agent/tools/*.py never needs to know monitoring exists.
"""
import json
import time
from typing import Any, Dict, List, Optional
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from sqlmodel import Session

from app.agent.monitoring_models import AgentRunLog
from app.common.llm import position_from_tags
from app.common.llm_usage import LLMFeature, log_llm_usage, usage_from_response
from app.database import engine
from app.logger import get_logger

logger = get_logger(__name__)

MAX_TEXT_CHARS = 2000


def _preview(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """Renders any value to a bounded string. Truncates rather than storing
    unbounded tool output / prompt text, so a single huge payload can't
    bloat agent_run_logs."""
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(s) <= limit:
        return s
    return f"{s[:limit]}…[truncated, {len(s)} chars total]"


class MonitoringCallbackHandler(AsyncCallbackHandler):
    """One instance per agent turn. Pass via config={"callbacks": [handler]}
    to agent.ainvoke(...) / agent.astream_events(...)."""

    def __init__(self) -> None:
        self.tool_calls: List[Dict[str, Any]] = []
        self._open: Dict[str, tuple] = {}  # run_id -> (entry dict, monotonic start)
        self.input_tokens: Optional[int] = None
        self.output_tokens: Optional[int] = None
        # Which model answered: the model of the turn's LAST completed LLM
        # call (the one that wrote the reply) and its chain position (0 =
        # primary, 1..n = backup index). `fallback_triggered` is set when any
        # step of the turn ran on a backup.
        self.model: Optional[str] = None
        self.model_position: Optional[int] = None
        self.fallback_triggered = False
        self._llm_runs: Dict[str, tuple] = {}  # run_id -> (model, position)
        # (model, position) -> [input, output], one llm_usage_logs row each
        self.usage_by_model: Dict[tuple, List[int]] = {}

    async def on_chat_model_start(
        self,
        serialized: dict,
        messages: Any,
        *,
        run_id: UUID,
        tags: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        position = position_from_tags(tags)
        if position:
            self.fallback_triggered = True
        self._llm_runs[str(run_id)] = ((metadata or {}).get("ls_model_name") or "unknown", position)

    async def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: UUID,
        inputs: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        entry: Dict[str, Any] = {
            "name": serialized.get("name", "unknown"),
            "args_preview": _preview(inputs if inputs is not None else input_str),
            "duration_ms": None,
            "output_preview": None,
            "error": None,
        }
        self.tool_calls.append(entry)
        self._open[str(run_id)] = (entry, time.monotonic())

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        opened = self._open.pop(str(run_id), None)
        if opened is None:
            return
        entry, started = opened
        entry["duration_ms"] = round((time.monotonic() - started) * 1000)
        entry["output_preview"] = _preview(output)

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        opened = self._open.pop(str(run_id), None)
        if opened is None:
            return
        entry, started = opened
        entry["duration_ms"] = round((time.monotonic() - started) * 1000)
        entry["error"] = str(error)

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        model, position = self._llm_runs.pop(str(run_id), ("unknown", 0))
        self.model, self.model_position = model, position
        tokens_in, tokens_out = usage_from_response(response)
        if tokens_in or tokens_out:
            self.input_tokens = (self.input_tokens or 0) + tokens_in
            self.output_tokens = (self.output_tokens or 0) + tokens_out
            totals = self.usage_by_model.setdefault((model, position), [0, 0])
            totals[0] += tokens_in
            totals[1] += tokens_out

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._llm_runs.pop(str(run_id), None)

    def finalize_incomplete_calls(self) -> None:
        """Sweeps any tool_start left without a matching end/error — e.g. an
        uncaught exception inside the tool that skipped both callbacks — so
        nothing lingers in _open and every persisted tool_calls entry has a
        definitive status instead of a silently-missing one. Call this
        before reading tool_calls for persistence."""
        for entry, started in self._open.values():
            entry["duration_ms"] = round((time.monotonic() - started) * 1000)
            entry["error"] = "unclosed (no on_tool_end/on_tool_error fired)"
        self._open.clear()


def log_agent_run(
    conversation_id: UUID,
    user_id: UUID,
    user_message: str,
    reply_text: str,
    status: str,
    error_message: Optional[str],
    latency_ms: int,
    handler: Optional[MonitoringCallbackHandler],
) -> None:
    """Persists one AgentRunLog row. Call via background_tasks.add_task(...)
    — never inline in the request path (see module docstring). Opens its own
    Session against the shared engine rather than reusing the request's
    session, since a background task can outlive the request's session
    lifecycle."""
    if handler is not None:
        handler.finalize_incomplete_calls()

    try:
        with Session(engine) as session:
            session.add(
                AgentRunLog(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    user_message=_preview(user_message),
                    reply_text=_preview(reply_text),
                    status=status,
                    error_message=error_message,
                    latency_ms=latency_ms,
                    input_tokens=handler.input_tokens if handler else None,
                    output_tokens=handler.output_tokens if handler else None,
                    tool_calls=handler.tool_calls if handler else [],
                    model=handler.model if handler else None,
                    model_position=handler.model_position if handler else None,
                    fallback_triggered=handler.fallback_triggered if handler else False,
                )
            )
            session.commit()
    except Exception:
        logger.exception("failed to write agent_run_log (monitoring only, chat unaffected)")

    # The turn's totals into the app-wide usage table, one row per model that
    # answered a step. Summed per turn here rather than a TokenUsageHandler on
    # the agent LLM, which would write a row per ReAct step from inside the
    # streaming loop.
    if handler is not None:
        for (model, position), (tokens_in, tokens_out) in handler.usage_by_model.items():
            log_llm_usage(LLMFeature.AGENT, model, tokens_in, tokens_out, user_id, position)
