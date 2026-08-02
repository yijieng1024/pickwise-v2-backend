import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.monitoring_models import AgentRunLog, AgentRunLogDetail, AgentRunLogSummary
from app.common.pagination_service import Page, PaginationParams, count_total, paginate
from app.common.search_service import apply_search, search_query
from app.common.sorting_service import SortDirection, apply_sort, sort_dir_query
from app.database import get_session
from app.users.auth import get_current_admin
from app.users.models import User

router = APIRouter(prefix="/agent/monitoring", tags=["Admin - Agent Monitoring"])

# Allow-list for ?sort_by= — see app.common.sorting_service.
RUN_SORTABLE_COLUMNS = {
    "created_at": AgentRunLog.created_at,
    "latency_ms": AgentRunLog.latency_ms,
}

# How many recent rows /stats aggregates over. Computed in Python rather than
# a raw JSONB aggregation query — simpler and correct at this data scale;
# not worth the SQL complexity yet (see app/common services for the same
# philosophy applied to pagination/search/sort).
STATS_WINDOW = 1000


def _to_summary(run: AgentRunLog, username: str) -> AgentRunLogSummary:
    return AgentRunLogSummary(
        id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        username=username,
        user_message=run.user_message,
        status=run.status,
        latency_ms=run.latency_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        tool_names=[t.get("name", "unknown") for t in run.tool_calls],
        tool_call_count=len(run.tool_calls),
        created_at=run.created_at,
    )


@router.get("/runs", response_model=Page[AgentRunLogSummary], dependencies=[Depends(get_current_admin)])
def list_agent_runs(
    search: Optional[str] = search_query("Matches the user's message"),
    status_filter: Optional[str] = Query(default=None, alias="status", description="success or error"),
    sort_by: Optional[str] = Query(default=None, description="One of: created_at, latency_ms"),
    sort_dir: SortDirection = sort_dir_query(default=SortDirection.desc),
    pagination: PaginationParams = Depends(),
    session: Session = Depends(get_session),
):
    """Browsable, searchable, sortable log of every Pico agent turn (admin only)."""
    statement = select(AgentRunLog)
    statement = apply_search(statement, search, [AgentRunLog.user_message])
    if status_filter:
        statement = statement.where(AgentRunLog.status == status_filter)

    total = count_total(session, statement)

    statement = apply_sort(statement, sort_by, sort_dir, RUN_SORTABLE_COLUMNS, AgentRunLog.created_at)
    runs = session.exec(paginate(statement, pagination)).all()

    user_ids = {r.user_id for r in runs}
    usernames = {}
    if user_ids:
        usernames = {
            u.id: u.username
            for u in session.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
        }

    items = [_to_summary(r, usernames.get(r.user_id, "unknown")) for r in runs]
    return Page(items=items, total=total, skip=pagination.skip, limit=pagination.limit)


@router.get("/runs/{run_id}", response_model=AgentRunLogDetail, dependencies=[Depends(get_current_admin)])
def get_agent_run(run_id: UUID, session: Session = Depends(get_session)):
    run = session.get(AgentRunLog, run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")

    user = session.get(User, run.user_id)
    return AgentRunLogDetail(
        id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        username=user.username if user else "unknown",
        user_message=run.user_message,
        reply_text=run.reply_text,
        status=run.status,
        error_message=run.error_message,
        latency_ms=run.latency_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        tool_calls=run.tool_calls,
        created_at=run.created_at,
    )


class ToolUsage(BaseModel):
    name: str
    count: int


class AgentMonitoringStats(BaseModel):
    total_runs: int
    runs_today: int
    error_count: int
    error_rate_pct: float
    avg_latency_ms: float
    tool_usage: list[ToolUsage]


@router.get("/stats", response_model=AgentMonitoringStats, dependencies=[Depends(get_current_admin)])
def get_agent_monitoring_stats(session: Session = Depends(get_session)):
    recent = session.exec(
        select(AgentRunLog).order_by(AgentRunLog.created_at.desc()).limit(STATS_WINDOW)  # type: ignore[union-attr]
    ).all()

    if not recent:
        return AgentMonitoringStats(
            total_runs=0, runs_today=0, error_count=0,
            error_rate_pct=0.0, avg_latency_ms=0.0, tool_usage=[],
        )

    today = datetime.now(timezone.utc).date()
    runs_today = sum(1 for r in recent if r.created_at.date() == today)
    error_count = sum(1 for r in recent if r.status == "error")

    tool_counter: Counter = Counter()
    for r in recent:
        for t in r.tool_calls:
            tool_counter[t.get("name", "unknown")] += 1

    return AgentMonitoringStats(
        total_runs=len(recent),
        runs_today=runs_today,
        error_count=error_count,
        error_rate_pct=round(error_count / len(recent) * 100, 1),
        avg_latency_ms=round(statistics.mean(r.latency_ms for r in recent), 1),
        tool_usage=[ToolUsage(name=name, count=count) for name, count in tool_counter.most_common()],
    )
