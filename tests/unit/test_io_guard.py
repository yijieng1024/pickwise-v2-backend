"""The unit tier's I/O guard blocks real databases and the network, and allows
SQLite and loopback (tests/_io_guard.py). The guard's checks are called
directly here: a real blocked attempt would also be recorded by the autouse
guard and fail this test, which is the guard working, not the test."""
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from tests._io_guard import BlockedIO, IOGuard


def test_a_real_database_is_blocked_and_recorded():
    guard = IOGuard()
    with pytest.raises(BlockedIO):
        guard._on_connect(SimpleNamespace(name="postgresql"), None, (), {"host": "db.example.supabase.co"})
    assert guard.violations == ["real database connection (postgresql, host db.example.supabase.co)"]


def test_sqlite_is_allowed():
    guard = IOGuard()
    guard._on_connect(SimpleNamespace(name="sqlite"), None, (), {})
    assert guard.violations == []
    # And for real, under the autouse guard.
    with create_engine("sqlite://").connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1


@pytest.mark.parametrize("host", ["generativelanguage.googleapis.com", "www.googleapis.com", "serpapi.com", "8.8.8.8"])
def test_the_network_is_blocked_and_recorded(host):
    guard = IOGuard()
    with pytest.raises(BlockedIO):
        guard._check_host(host, "DNS lookup")
    assert guard.violations == [f"DNS lookup to {host}"]


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_is_allowed(host):
    guard = IOGuard()
    guard._check_host(host, "network connection")
    assert guard.violations == []


def test_the_guard_is_active_for_unit_tests():
    """The autouse fixture has replaced socket.getaddrinfo with the guard's."""
    assert socket.getaddrinfo.__name__ == "getaddrinfo" and socket.getaddrinfo.__module__ == "tests._io_guard"
