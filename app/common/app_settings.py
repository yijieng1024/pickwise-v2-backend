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

# Keys are built from constants, never free text at the call site — a typo'd
# key silently reads as "unset" and the default wins, which is invisible. The
# model keys live in llm_config (`primary_key(use)` / `backups_key(use)`).

# Bumped on every write so in-process caches of settings (llm_config) can tell
# their copy is stale without a query.
_generation = 0


def generation() -> int:
    return _generation


def _bump() -> None:
    global _generation
    _generation += 1


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"  # type: ignore

    key: str = Field(primary_key=True, max_length=100)
    # 2000: room for an ordered JSON list of backup model specs.
    value: str = Field(max_length=2000)
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
    _bump()
    return row


def delete_setting(session: Session, key: str) -> None:
    """Remove the row so the consumer's default applies again. Commits."""
    row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    if row:
        session.delete(row)
        session.commit()
    _bump()
