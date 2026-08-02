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
        for gen_list in response.generations:
            for gen in gen_list:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if not usage:
                    continue
                self.input_tokens = (self.input_tokens or 0) + (usage.get("input_tokens") or 0)
                self.output_tokens = (self.output_tokens or 0) + (usage.get("output_tokens") or 0)

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
                )
            )
            session.commit()
    except Exception:
        logger.exception("failed to write agent_run_log (monitoring only, chat unaffected)")
