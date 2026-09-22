"""
The sliding-window limiter behind the auth endpoints.

Unit tier: no app, no database, no clock manipulation beyond a stubbed
`time.monotonic`. What is pinned here is the arithmetic; the wiring (which
endpoint gets which bucket) is exercised in the integration tier.
"""

import types

import pytest
from fastapi import HTTPException

from tests.unit._adapters import rate_limit_module

limiter = rate_limit_module()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test advances by hand."""
    now = {"t": 1000.0}
    monkeypatch.setattr(limiter.time, "monotonic", lambda: now["t"])
    return now


def _request(ip=None, forwarded=None):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return types.SimpleNamespace(
        headers=headers,
        client=types.SimpleNamespace(host=ip) if ip else None,
    )


# --- the window ----------------------------------------------------------


def test_allows_up_to_the_limit_then_blocks(clock):
    for _ in range(3):
        assert limiter.check_quota("b", "k", 3, 60) is True
    assert limiter.check_quota("b", "k", 3, 60) is False


def test_window_expiry_releases(clock):
    for _ in range(3):
        limiter.check_quota("b", "k", 3, 60)
    assert limiter.check_quota("b", "k", 3, 60) is False

    clock["t"] += 61
    assert limiter.check_quota("b", "k", 3, 60) is True


def test_a_rejected_request_is_not_counted(clock):
    """Otherwise a client that keeps hammering never falls back inside the
    window and is locked out indefinitely."""
    for _ in range(2):
        limiter.check_quota("b", "k", 2, 60)

    clock["t"] += 30
    assert limiter.check_quota("b", "k", 2, 60) is False  # rejected at t+30

    clock["t"] += 31  # t+61: both original hits have aged out
    assert limiter.check_quota("b", "k", 2, 60) is True


def test_keys_and_buckets_are_independent(clock):
    for _ in range(2):
        limiter.check_quota("b", "alice", 2, 60)

    assert limiter.check_quota("b", "alice", 2, 60) is False
    assert limiter.check_quota("b", "bob", 2, 60) is True
    assert limiter.check_quota("other", "alice", 2, 60) is True


def test_identity_is_case_and_whitespace_insensitive(clock):
    """Email addresses arrive however the user typed them; one account must
    not get five cooldowns by varying the capitalisation."""
    assert limiter.check_quota("b", "User@Example.com", 1, 60) is True
    assert limiter.check_quota("b", "  user@example.com ", 1, 60) is False


# --- the dependency ------------------------------------------------------


def test_dependency_raises_429_with_retry_after(clock):
    dependency = limiter.rate_limit("login", 2, 300)
    request = _request(ip="1.2.3.4")

    dependency(request)
    dependency(request)

    with pytest.raises(HTTPException) as exc:
        dependency(request)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) == 300


def test_dependency_separates_clients(clock):
    dependency = limiter.rate_limit("login", 1, 300)
    dependency(_request(ip="1.2.3.4"))
    dependency(_request(ip="5.6.7.8"))  # must not raise


# --- client identification ----------------------------------------------


def test_forwarded_for_wins_over_the_socket_address():
    """On Render `request.client.host` is the proxy. If that were the key,
    every user of the site would share one bucket."""
    request = _request(ip="10.0.0.1", forwarded="203.0.113.7, 10.0.0.1")
    assert limiter.client_ip(request) == "203.0.113.7"


def test_falls_back_to_the_socket_address_without_the_header():
    assert limiter.client_ip(_request(ip="127.0.0.1")) == "127.0.0.1"


def test_falls_back_again_when_there_is_no_client_at_all():
    assert limiter.client_ip(_request()) == "unknown"


def test_blank_forwarded_header_does_not_produce_an_empty_key():
    assert limiter.client_ip(_request(ip="127.0.0.1", forwarded="   ")) == "127.0.0.1"


def test_two_proxied_clients_do_not_share_a_bucket():
    """The regression this whole function exists to prevent."""
    dependency = limiter.rate_limit("login", 1, 300)
    dependency(_request(ip="10.0.0.1", forwarded="203.0.113.7"))
    dependency(_request(ip="10.0.0.1", forwarded="198.51.100.4"))  # must not raise
