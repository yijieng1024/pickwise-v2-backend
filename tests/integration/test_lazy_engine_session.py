"""
Postgres twin of tests/unit/test_lazy_engine_session.py: a session bound to the
_Lazy engine proxy runs every statement on one backend, and sees its own
flushed rows. Uses its own proxy over the test engine -- the app's proxy is
deliberately never resolved in this tier (see conftest._app_client).
"""
from sqlalchemy import text
from sqlmodel import Session

from app.config import _Lazy


def test_lazy_engine_session_uses_one_backend(engine):
    proxy = _Lazy(lambda: engine, "Engine")
    with Session(proxy) as s:
        pids = {s.execute(text("select pg_backend_pid()")).scalar() for _ in range(3)}
        s.execute(text("create temp table _probe (x int)"))
        s.execute(text("insert into _probe values (1)"))
        # A temp table is per-connection: a second backend would not even see it.
        assert s.execute(text("select count(*) from _probe")).scalar() == 1
        s.rollback()
    assert len(pids) == 1
