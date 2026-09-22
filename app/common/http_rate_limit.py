"""
Request throttling for the auth endpoints.

NOT the same thing as `app/common/rate_limit.py`, which paces our OUTBOUND
Gemini calls against a provider quota. This module throttles INBOUND HTTP
requests against abuse: credential stuffing on /auth/login, and burning the
Brevo daily send quota through /auth/forgot-password.

WHY NOT slowapi. Its storage backend defaults to in-process memory, exactly
like the dict below, and the one thing it adds that this cannot do -- a shared
Redis/Memcached backend, so counters survive a restart and are consistent
across instances -- needs infrastructure this deployment does not have. Render
free tier is a single instance with no Redis, so slowapi would be a dependency
for a dict of deques.

# ponytail: in-process counters; they reset on every deploy/restart and would
# be per-instance if this ever scales out. Swap the two `_hits` lookups for a
# Redis backend (or slowapi over Redis) behind the same Depends() if either
# becomes true.
"""

import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

from fastapi import HTTPException, Request, status

# key -> timestamps of the requests counted against it, oldest first.
_hits: Dict[str, Deque[float]] = {}

# An unbounded dict keyed on client IP is a memory leak with a public endpoint
# in front of it. Well past any legitimate working set; when it is hit, the
# keys with nothing left inside the window are dropped.
_MAX_TRACKED_KEYS = 10_000


def _prune_all(now: float) -> None:
    """Drop keys whose window has emptied. Only runs when the dict is large."""
    for key in [k for k, hits in _hits.items() if not hits or now - hits[-1] > 3600]:
        del _hits[key]


def _record(key: str, limit: int, window: float) -> Optional[float]:
    """
    Count one hit against `key`.

    Returns None when the request is within the limit, or the number of
    seconds until the oldest hit falls out of the window when it is not. A
    rejected request is NOT counted -- otherwise a client that keeps hammering
    can never come back inside the window.
    """
    now = time.monotonic()
    hits = _hits.setdefault(key, deque())

    while hits and now - hits[0] >= window:
        hits.popleft()

    if len(hits) >= limit:
        return window - (now - hits[0])

    if len(_hits) > _MAX_TRACKED_KEYS:
        _prune_all(now)

    hits.append(now)
    return None


def client_ip(request: Request) -> str:
    """
    The real caller's address, behind Render's proxy.

    `request.client.host` is the PROXY on Render, so keying on it puts every
    user of the site into one bucket and the first ten logins lock out the
    eleventh. Uvicorn can rewrite it from `X-Forwarded-For`, but only for
    proxies listed in `--forwarded-allow-ips`, which defaults to 127.0.0.1 --
    Render's proxy is not, and widening it to "*" would let any direct caller
    spoof `request.client.host` for the whole application, not just here.

    So the header is read explicitly, and only in this module. The FIRST entry
    is the originating client; the rest are proxies appended on the way in.

    A spoofed header lets an attacker pick their own bucket, which is the
    standard trade for a limiter behind a proxy that does not authenticate its
    own hops: this is abuse-throttling, not a security boundary. The
    per-account limits are the ones that hold regardless, since the attacker
    does not control which account they are attacking.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    # No header: running locally or straight against uvicorn. Falls back to the
    # socket address, and to a shared bucket only when even that is absent
    # (ASGI transports without a client, e.g. some test harnesses).
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, limit: int, window: int) -> Callable[[Request], None]:
    """
    A FastAPI dependency: at most `limit` requests per `window` seconds, per
    client IP, per `bucket`.

        @router.post("/login", dependencies=[Depends(rate_limit("login", 10, 300))])
    """

    def dependency(request: Request) -> None:
        retry_after = _record(f"{bucket}:{client_ip(request)}", limit, window)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please wait a moment and try again.",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )

    return dependency


def check_quota(bucket: str, identity: str, limit: int, window: int) -> bool:
    """
    Count one hit against an identity that is only known after the body is
    parsed (an email address, a login identifier). True when it is allowed.

    Returns a bool instead of raising, because every caller of this is an
    endpoint that must answer identically whether or not it did the work --
    a 429 here would tell an attacker that the address is worth retrying, and
    on /auth/login it would distinguish a real account from a made-up one.
    """
    return _record(f"{bucket}:{identity.strip().lower()}", limit, window) is None


def reset() -> None:
    """Clear all counters. For tests; nothing in the app calls it."""
    _hits.clear()
