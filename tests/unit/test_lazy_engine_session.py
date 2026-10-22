"""
Session(<lazy engine proxy>) must use ONE connection per transaction.

app.database.engine is a _Lazy proxy. SQLAlchemy files a session's connection
under the real Engine and looks it up with the bind it was given -- the proxy --
so while the proxy hashed by identity every statement opened a fresh pooled
connection: production pool exhaustion, and flushed writes invisible to the
same session's next query. The integration tier could not catch it, because it
swaps every module's `engine` for a Connection before any test runs.

File-backed SQLite with a QueuePool, so the pool really hands out separate
connections, as Postgres's does.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
from sqlmodel import Session

from ._adapters import _Lazy


def _proxy(tmp_path):
    real = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", poolclass=QueuePool)
    with real.begin() as conn:
        conn.execute(text("create table t (x int)"))
    return real, _Lazy(lambda: real, "Engine")


def test_one_session_uses_one_connection(tmp_path):
    real, proxy = _proxy(tmp_path)
    with Session(proxy) as s:
        conns = {id(s.connection().connection.dbapi_connection) for _ in range(3)}
        for _ in range(3):
            s.execute(text("select 1"))
        assert len(conns) == 1
        assert real.pool.checkedout() == 1


def test_flushed_write_is_visible_to_a_later_query(tmp_path):
    _, proxy = _proxy(tmp_path)
    with Session(proxy) as s:
        s.execute(text("insert into t values (1)"))
        assert s.execute(text("select count(*) from t")).scalar() == 1


def test_equality_never_builds_the_object():
    built = []
    proxy = _Lazy(lambda: built.append(1) or object(), "X")
    assert proxy == proxy
    assert proxy != object()
    assert not built
