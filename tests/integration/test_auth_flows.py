"""
Tier 4 -- the auth email flows, end to end through the real routes.

What the unit tier cannot cover: that the endpoints answer identically across
branches that took different code paths (enumeration), that a spent reset
token is refused by the route and not merely by the decoder, and that the
limiter is actually wired to the endpoints rather than just correct in
isolation.

Every email sender is replaced with a recorder, so nothing reaches Brevo and
nothing is charged against the 300/day quota.
"""

import uuid

import pytest

from app.common import http_rate_limit as limiter
from app.users import router as users_router
from app.users.auth import create_password_reset_token, get_password_hash
from app.users.models import User

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_limiter():
    """Counters are process-global, so one test's login attempts would
    otherwise spend the next test's budget."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _secret_key(app_settings):
    """
    The tier builds `Settings` with `model_construct` and NO secrets, so
    `settings.secret_key` is simply absent -- and these routes mint JWTs.
    A dummy value is correct here precisely because it is not a credential to
    anything: it signs tokens this test then verifies itself.
    """
    object.__setattr__(
        app_settings._resolve(), "secret_key", "integration-tier-signing-key"
    )
    yield


@pytest.fixture
def sent(monkeypatch):
    """Record every send instead of performing it."""
    log = {"verification": [], "reset": [], "google_notice": []}

    monkeypatch.setattr(
        users_router, "send_verification_email",
        lambda email, token: log["verification"].append((email, token)) or True,
    )
    monkeypatch.setattr(
        users_router, "send_password_reset_email",
        lambda email_to, token: log["reset"].append((email_to, token)) or True,
    )
    monkeypatch.setattr(
        users_router, "send_google_account_notice_email",
        lambda email_to: log["google_notice"].append(email_to) or True,
    )
    return log


def _make_user(session, *, verified=True, password="OldPassw0rd", google=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"u-{suffix}",
        email=f"u-{suffix}@example.com",
        password=None if google else get_password_hash(password),
        auth_provider="google" if google else "local",
        provider_sub=f"sub-{suffix}" if google else None,
        is_verified=verified,
        status="active",
        role="user",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# --------------------------------------------------------------------------
# resend-verification answers identically for every branch
# --------------------------------------------------------------------------


def test_resend_verification_is_indistinguishable_across_branches(
    api_client, session, sent
):
    """Unknown / unverified / already-verified / Google must be one response.
    Any difference is a free "is this address registered" oracle."""
    unverified = _make_user(session, verified=False)
    verified = _make_user(session, verified=True)
    google = _make_user(session, google=True, verified=True)
    unknown = f"nobody-{uuid.uuid4().hex[:8]}@example.com"

    responses = [
        api_client.post("/api/v2/auth/resend-verification", json={"email": address})
        for address in (unverified.email, verified.email, google.email, unknown)
    ]

    assert {r.status_code for r in responses} == {202}
    assert len({r.text for r in responses}) == 1, "bodies differ between branches"

    # ...and only the branch that should have mailed, did.
    assert [address for address, _ in sent["verification"]] == [unverified.email]


def test_resend_verification_token_verifies_the_account(api_client, session, sent):
    user = _make_user(session, verified=False)

    api_client.post("/api/v2/auth/resend-verification", json={"email": user.email})
    (_, token), = sent["verification"]

    r = api_client.get(f"/api/v2/auth/verify-email?token={token}")
    assert r.status_code == 200
    session.refresh(user)
    assert user.is_verified is True


def test_resend_verification_cooldown_is_silent(api_client, session, sent):
    """The 60s per-address cooldown must not change the response -- a 429 here
    would confirm the address is worth retrying."""
    user = _make_user(session, verified=False)

    first = api_client.post("/api/v2/auth/resend-verification", json={"email": user.email})
    second = api_client.post("/api/v2/auth/resend-verification", json={"email": user.email})

    assert first.status_code == second.status_code == 202
    assert first.text == second.text
    assert len(sent["verification"]) == 1, "cooldown did not suppress the second send"


# --------------------------------------------------------------------------
# login: the unverified 403 carries a machine-readable code
# --------------------------------------------------------------------------


def test_unverified_login_returns_a_code_the_frontend_can_branch_on(
    api_client, session
):
    user = _make_user(session, verified=False, password="OldPassw0rd")

    r = api_client.post(
        "/api/v2/auth/login",
        data={"username": user.email, "password": "OldPassw0rd"},
    )

    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "email_unverified"
    assert detail["message"]


# --------------------------------------------------------------------------
# forgot-password / reset-password
# --------------------------------------------------------------------------


def test_forgot_password_is_indistinguishable_across_branches(
    api_client, session, sent
):
    local = _make_user(session)
    google = _make_user(session, google=True)
    unknown = f"nobody-{uuid.uuid4().hex[:8]}@example.com"

    responses = [
        api_client.post("/api/v2/auth/forgot-password", json={"email": address})
        for address in (local.email, google.email, unknown)
    ]

    assert {r.status_code for r in responses} == {202}
    assert len({r.text for r in responses}) == 1

    assert [address for address, _ in sent["reset"]] == [local.email]
    assert sent["google_notice"] == [google.email]


def test_reset_password_does_not_404_on_an_unknown_address(api_client, session):
    """The leak this endpoint used to have: a 404 "User not found" told the
    caller their guessed address was NOT registered."""
    token = create_password_reset_token(
        email=f"ghost-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=get_password_hash("Whatever1"),
    )

    r = api_client.post(
        "/api/v2/auth/reset-password",
        json={"token": token, "new_password": "NewPassw0rd"},
    )

    assert r.status_code == 400
    assert r.json()["detail"] == users_router.INVALID_RESET_MESSAGE


def test_reset_password_unknown_address_matches_a_garbage_token(api_client):
    """Same status AND same body, or the difference is the oracle."""
    ghost = api_client.post(
        "/api/v2/auth/reset-password",
        json={
            "token": create_password_reset_token(
                email=f"ghost-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=get_password_hash("Whatever1"),
            ),
            "new_password": "NewPassw0rd",
        },
    )
    garbage = api_client.post(
        "/api/v2/auth/reset-password",
        json={"token": "not-a-jwt", "new_password": "NewPassw0rd"},
    )

    assert ghost.status_code == garbage.status_code == 400
    assert ghost.json() == garbage.json()


def test_a_reset_link_works_exactly_once(api_client, session, sent):
    """The single-use property. The same token, replayed inside its 15-minute
    window, must fail once the password it was minted against is gone."""
    user = _make_user(session, password="OldPassw0rd")

    api_client.post("/api/v2/auth/forgot-password", json={"email": user.email})
    (_, token), = sent["reset"]

    first = api_client.post(
        "/api/v2/auth/reset-password",
        json={"token": token, "new_password": "NewPassw0rd"},
    )
    assert first.status_code == 200

    replay = api_client.post(
        "/api/v2/auth/reset-password",
        json={"token": token, "new_password": "Attacker1Pass"},
    )
    assert replay.status_code == 400
    assert replay.json()["detail"] == users_router.INVALID_RESET_MESSAGE


def test_old_links_die_when_the_password_changes_by_another_route(
    api_client, session, sent
):
    """Two links outstanding; spending either must kill the other."""
    user = _make_user(session, password="OldPassw0rd")

    api_client.post("/api/v2/auth/forgot-password", json={"email": user.email})
    limiter.reset()  # the 60s cooldown would otherwise suppress the second
    api_client.post("/api/v2/auth/forgot-password", json={"email": user.email})
    assert len(sent["reset"]) == 2
    (_, first_token), (_, second_token) = sent["reset"]

    spent = api_client.post(
        "/api/v2/auth/reset-password",
        json={"token": second_token, "new_password": "NewPassw0rd"},
    )
    assert spent.status_code == 200

    stale = api_client.post(
        "/api/v2/auth/reset-password",
        json={"token": first_token, "new_password": "Attacker1Pass"},
    )
    assert stale.status_code == 400


# --------------------------------------------------------------------------
# the limiter is actually wired up
# --------------------------------------------------------------------------


def test_login_rate_limit_trips_per_ip(api_client, session):
    user = _make_user(session, password="OldPassw0rd")

    statuses = [
        api_client.post(
            "/api/v2/auth/login",
            data={"username": user.email, "password": "WrongPassw0rd"},
        ).status_code
        for _ in range(12)
    ]

    assert statuses[:10] == [401] * 10
    assert 429 in statuses[10:], "the per-IP login limit is not wired to the route"


def test_login_account_limit_answers_401_not_429(api_client, session):
    """The per-account limit must be indistinguishable from a wrong password,
    or it confirms which accounts exist."""
    user = _make_user(session, password="OldPassw0rd")

    # Spend the account's budget from one "IP"...
    for _ in range(10):
        api_client.post(
            "/api/v2/auth/login",
            data={"username": user.email, "password": "WrongPassw0rd"},
        )

    # ...then come back from a different one, with the CORRECT password. The
    # per-IP counter is fresh; the per-account one is not.
    r = api_client.post(
        "/api/v2/auth/login",
        data={"username": user.email, "password": "OldPassw0rd"},
        headers={"X-Forwarded-For": "203.0.113.99"},
    )

    assert r.status_code == 401
    assert "Retry-After" not in r.headers


def test_forgot_password_rate_limit_trips_per_ip(api_client, session, sent):
    address = f"nobody-{uuid.uuid4().hex[:8]}@example.com"

    statuses = [
        api_client.post(
            "/api/v2/auth/forgot-password", json={"email": address}
        ).status_code
        for _ in range(12)
    ]

    assert statuses[:10] == [202] * 10
    assert 429 in statuses[10:]


def test_proxied_clients_do_not_share_a_bucket(api_client, session):
    """The Render regression: if the limiter keyed on request.client.host,
    every user of the site would share one budget."""
    user = _make_user(session, password="OldPassw0rd")

    for _ in range(10):
        api_client.post(
            "/api/v2/auth/login",
            data={"username": f"other-{uuid.uuid4().hex[:8]}", "password": "x"},
            headers={"X-Forwarded-For": "203.0.113.1"},
        )

    # A different client, same proxy. Must not be locked out.
    r = api_client.post(
        "/api/v2/auth/login",
        data={"username": user.email, "password": "OldPassw0rd"},
        headers={"X-Forwarded-For": "198.51.100.2"},
    )
    assert r.status_code != 429
