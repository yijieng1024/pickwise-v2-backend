"""
No real database and no real network outside tests/integration.

The unit tier (tests/unit, plus the golden and other root-level tests that run
in the unit CI job) must run with no database, no network and no API key. That
was a convention, and on 2026-10-26 it failed twice over: four tests reached
the real Gemini embedder through the search tool's query-vector prefetch, and
the new embedding usage logger then wrote each call to whatever DATABASE_URL
pointed at, which on a developer machine is production. CI never noticed,
because with no secrets both calls failed and the code under test swallowed
the failures.

So the guard does not trust exceptions to surface. Every blocked attempt is
recorded, and the test fails at teardown if anything was recorded, even if the
code under test caught the error and carried on.

Allowed: SQLite (in memory or a temp file) and loopback sockets, which several
tests use on purpose (a pooled SQLite file to watch connection checkouts, a
local hanging server to prove timeouts fire).
"""
import socket

from sqlalchemy import event
from sqlalchemy.engine import Engine

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


class BlockedIO(RuntimeError):
    """A unit test tried to reach a real database or the network."""


class IOGuard:
    def __init__(self):
        self.violations: list[str] = []

    def _block(self, what: str):
        self.violations.append(what)
        raise BlockedIO(f"{what} — not allowed outside tests/integration (see tests/_io_guard.py)")

    # --- database ------------------------------------------------------------

    def _on_connect(self, dialect, _conn_rec, _cargs, cparams):
        if dialect.name != "sqlite":
            host = cparams.get("host") or cparams.get("dsn") or "?"
            self._block(f"real database connection ({dialect.name}, host {host})")

    # --- network -------------------------------------------------------------

    def _check_host(self, host, what: str):
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if host is None or str(host) in _LOOPBACK or str(host).startswith("127."):
            return
        self._block(f"{what} to {host}")

    def install(self, monkeypatch):
        event.listen(Engine, "do_connect", self._on_connect)

        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_getaddrinfo = socket.getaddrinfo
        guard = self

        def connect(sock, address):
            if isinstance(address, tuple):
                guard._check_host(address[0], "network connection")
            return real_connect(sock, address)

        def connect_ex(sock, address):
            if isinstance(address, tuple):
                guard._check_host(address[0], "network connection")
            return real_connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            # DNS for any non-local name: httpx, gRPC and psycopg all resolve first.
            guard._check_host(host, "DNS lookup")
            return real_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    def uninstall(self):
        if event.contains(Engine, "do_connect", self._on_connect):
            event.remove(Engine, "do_connect", self._on_connect)
