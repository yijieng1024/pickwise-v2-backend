"""
Runtime settings an admin can change without a deploy.

One key/value table, read on use. Deliberately NOT part of `app/config.py`:
those are secrets and infrastructure, set on Render and validated at startup.
These are operational choices a human makes while watching a job fail.

Values are opaque strings; each consumer owns its own allow-list and default,
so an unknown key or a stale value degrades to the code default rather than
propagating a bad string into an API call.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, select

# Keys are constants, not free text at the call site — a typo'd key silently
# reads as "unset" and the default wins, which is invisible.
PROCESSOR_MODEL_KEY = "processor.model"


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"  # type: ignore

    key: str = Field(primary_key=True, max_length=100)
    value: str = Field(max_length=200)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def get_setting(session: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> AppSetting:
    """Upsert. Commits — callers are admin writes, not part of a larger unit."""
    row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = AppSetting(key=key, value=value)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
