from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.common.llm_usage import LLMUsageLog
from app.database import get_session
from app.users.auth import get_current_admin
from app.users.models import User

router = APIRouter(
    prefix="/admin/llm-usage",
    tags=["Admin - LLM Usage"],
    dependencies=[Depends(get_current_admin)],
)


class UsageRow(BaseModel):
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


class UserUsageRow(UsageRow):
    user_id: Optional[UUID] = None


class LLMUsageReport(BaseModel):
    days: int
    since: date
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    by_feature: list[UsageRow]
    by_model: list[UsageRow]
    by_day: list[UsageRow]
    by_user: list[UserUsageRow]


_IN = func.coalesce(func.sum(LLMUsageLog.input_tokens), 0)
_OUT = func.coalesce(func.sum(LLMUsageLog.output_tokens), 0)
_CALLS = func.count(LLMUsageLog.id)


def _row(key, calls, tokens_in, tokens_out) -> dict:
    return {
        "key": str(key), "calls": calls,
        "input_tokens": tokens_in, "output_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
    }


@router.get("", response_model=LLMUsageReport)
def get_llm_usage(
    days: int = Query(default=7, ge=1, le=365, description="Window, counted back from today (UTC)"),
    session: Session = Depends(get_session),
):
    """Token totals across every LLM call (agent, processor, review chunks),
    grouped by feature, model, UTC day and user. Batch-job
    calls have no user and appear under `user_id: null`."""
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    in_window = LLMUsageLog.created_at >= datetime.combine(since, datetime.min.time())

    def grouped(col):
        return session.exec(
            select(col, _CALLS, _IN, _OUT).where(in_window).group_by(col).order_by((_IN + _OUT).desc())
        ).all()

    day = func.date(LLMUsageLog.created_at)
    by_day = session.exec(
        select(day, _CALLS, _IN, _OUT).where(in_window).group_by(day).order_by(day.desc())
    ).all()

    users = session.exec(
        select(LLMUsageLog.user_id, func.max(User.username), _CALLS, _IN, _OUT)
        .join(User, User.id == LLMUsageLog.user_id, isouter=True)
        .where(in_window)
        .group_by(LLMUsageLog.user_id)
        .order_by((_IN + _OUT).desc())
    ).all()

    total_calls, total_in, total_out = session.exec(select(_CALLS, _IN, _OUT).where(in_window)).one()

    return LLMUsageReport(
        days=days, since=since,
        calls=total_calls, input_tokens=total_in, output_tokens=total_out,
        total_tokens=total_in + total_out,
        by_feature=[UsageRow(**_row(*r)) for r in grouped(LLMUsageLog.feature)],
        by_model=[UsageRow(**_row(*r)) for r in grouped(LLMUsageLog.model)],
        by_day=[UsageRow(**_row(*r)) for r in by_day],
        by_user=[
            UserUsageRow(user_id=uid, **_row(name or "system", c, i, o))
            for uid, name, c, i, o in users
        ],
    )
