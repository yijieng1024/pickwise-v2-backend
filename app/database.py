"""
database.py
-----------
Engine, pool configuration and session lifecycle.

Pool sizing — why these are set explicitly rather than left at SQLAlchemy's
defaults. The defaults are `pool_size=5, max_overflow=10`, i.e. 15 connections,
and that is what produced

    QueuePool limit of size 5 overflow 10 reached, connection timed out

in production. Three things share this one pool: ordinary requests, SSE chat
turns, and background jobs, and the admin dashboard alone fires ten requests in
parallel on load. The connections themselves are cheap; what is not cheap is
*holding* one while doing non-database work, so the rules are:

- Request handlers take a connection via `Depends(get_session)` for the length
  of the response. For a `StreamingResponse` that means the whole stream, so
  endpoints that stream an LLM turn must **not** use it — see the scoped
  sessions in `agent/router.py`.
- Anything outside the request cycle (background tasks, job workers, agent
  tools) opens its own short-lived session with `session_scope()` and closes it
  before doing slow work.

The ceiling is the Postgres server's `max_connections`, not this file: the
database is a Supabase instance reached on the direct port (5432), where the
smaller tiers cap out around 60 and Supabase's own services take a share.
`DB_POOL_SIZE + DB_MAX_OVERFLOW` is the most this process can open, so keep the
sum well under that cap — and multiply by the number of app instances if this
is ever scaled out horizontally. Moving to Supabase's transaction pooler
(port 6543) is the other way up, and would need `poolclass=NullPool` here.
"""

import os
from contextlib import contextmanager
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unparseable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


#: Connections kept open between requests.
POOL_SIZE = _int_env("DB_POOL_SIZE", 10)
#: Extra connections opened under load and closed again once returned.
MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 20)
#: Seconds a caller waits for a free connection before raising TimeoutError.
POOL_TIMEOUT = _int_env("DB_POOL_TIMEOUT", 30)
#: Recycle below any idle-connection cutoff the database or a proxy applies.
POOL_RECYCLE = _int_env("DB_POOL_RECYCLE", 1800)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_recycle=POOL_RECYCLE,
    # Managed Postgres drops idle connections without telling the client, and a
    # dead one is only discovered when a query fails on it. pre_ping spends one
    # trivial round trip per checkout to hand out a connection that is known
    # good; without it a quiet period is followed by a burst of
    # OperationalError, which is a different bug that looks like this one.
    pool_pre_ping=True,
    # LIFO hands back the most recently used connection, so a pool that grew
    # during a burst lets its tail go idle and get recycled rather than
    # round-robining traffic across every connection and keeping all of them
    # alive.
    pool_use_lifo=True,
)


def init_db():
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()

    SQLModel.metadata.create_all(engine)


def get_session():
    """Request-scoped session. FastAPI closes it when the response is sent."""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope(*, expire_on_commit: bool = True) -> Iterator[Session]:
    """
    Short-lived session for code outside the request cycle.

    Use it to bracket the database work and nothing else — open it, read or
    write, close it, *then* call the LLM or the scraper. Holding one across
    slow work is what exhausts the pool.

    `expire_on_commit=False` keeps the objects loaded in this scope readable
    after it closes, which is what lets a caller commit here and still pass the
    rows to something that runs after the connection is back in the pool.
    Detached rows are a snapshot: they will not see later writes, and writing
    to them does nothing until they are merged into a live session.
    """
    with Session(engine, expire_on_commit=expire_on_commit) as session:
        yield session
