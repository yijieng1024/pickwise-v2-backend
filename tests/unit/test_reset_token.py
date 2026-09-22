"""
The password-reset token's single-use fingerprint.

Unit tier: settings are stubbed, so no SECRET_KEY and no database. The
property under test is arithmetic over the stored hash, which is exactly why
it can be pinned without either.
"""

import types

import pytest

from tests.unit._adapters import auth_module

auth_module = auth_module()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "settings",
        types.SimpleNamespace(
            secret_key="unit-test-secret-long-enough-for-hs256",
            algorithm="HS256",
            email_verification_token_expire_hours=1,
        ),
    )


# Two real bcrypt-shaped digests. Literals rather than live hashing: bcrypt is
# deliberately slow, and nothing here depends on them being verifiable.
HASH_A = "$2b$12$abcdefghijklmnopqrstuuKPzQ1hN0mS0h7qYkQJ0wZ4Q1vG3xkK"
HASH_B = "$2b$12$zyxwvutsrqponmlkjihgfeDcBa9876543210ZyXwVuTsRqPoNmL"


def _decode(token):
    return auth_module.verify_password_reset_token(token)


def test_token_round_trips_with_the_hash_it_was_minted_from():
    token = auth_module.create_password_reset_token("user@example.com", HASH_A)

    decoded = _decode(token)
    assert decoded is not None
    email, fingerprint = decoded
    assert email == "user@example.com"
    assert fingerprint == auth_module.password_reset_fingerprint(HASH_A)


def test_fingerprint_stops_matching_once_the_password_changes():
    """The single-use property: after a reset, `users.password` holds a new
    digest, and the fingerprint baked into every outstanding link no longer
    matches it."""
    token = auth_module.create_password_reset_token("user@example.com", HASH_A)
    _, fingerprint = _decode(token)

    assert fingerprint != auth_module.password_reset_fingerprint(HASH_B)


def test_fingerprint_is_not_the_hash():
    """It travels in an email and a URL bar; the bcrypt digest must not."""
    fingerprint = auth_module.password_reset_fingerprint(HASH_A)

    assert HASH_A not in fingerprint
    assert fingerprint not in HASH_A
    assert len(fingerprint) == 16


def test_fingerprint_handles_a_passwordless_account():
    """Google-only accounts have `password = None`. The helper must not blow
    up on it — reset-password calls it on whatever the row holds."""
    assert auth_module.password_reset_fingerprint(None) == (
        auth_module.password_reset_fingerprint("")
    )


def test_wrong_scope_is_rejected():
    """An access token or a verification token must not open a reset."""
    verification = auth_module.create_email_verification_token("user@example.com")
    assert _decode(verification) is None


def test_garbage_token_is_rejected():
    assert _decode("not-a-jwt") is None
    assert _decode("") is None


def test_token_without_a_fingerprint_is_rejected():
    """Tokens minted before `fp` existed cannot be proven single-use, so they
    are refused rather than grandfathered."""
    import jwt
    from datetime import datetime, timedelta, timezone

    legacy = jwt.encode(
        {
            "sub": "user@example.com",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
            "scope": "password_reset",
        },
        "unit-test-secret-long-enough-for-hs256",
        algorithm="HS256",
    )
    assert _decode(legacy) is None
