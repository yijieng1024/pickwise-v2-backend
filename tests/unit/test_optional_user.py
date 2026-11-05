"""get_optional_user (app/users/auth.py): a public endpoint that personalizes when a
token is sent. No token, a bad token or an inactive account is None, never a 401."""
import types
from datetime import timedelta

import pytest

from tests.unit._adapters import auth_module

auth = auth_module()


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch):
    monkeypatch.setattr(auth, "settings", types.SimpleNamespace(
        secret_key="unit-test-secret-long-enough-for-hs256", algorithm="HS256"))


class _Session:
    def __init__(self, user):
        self.user = user

    def get(self, model, key):
        return self.user if self.user and str(self.user.id) == key else None


USER = types.SimpleNamespace(id="u-1", status="active")


def _token(sub="u-1"):
    return auth.create_access_token({"sub": sub}, timedelta(minutes=5))


def test_a_valid_token_is_the_user():
    assert auth.get_optional_user(_token(), _Session(USER)) is USER


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "x.y.z"])
def test_no_token_or_a_bad_one_is_none(token):
    assert auth.get_optional_user(token, _Session(USER)) is None


def test_an_unknown_or_inactive_account_is_none():
    assert auth.get_optional_user(_token("u-2"), _Session(USER)) is None
    assert auth.get_optional_user(_token(), _Session(types.SimpleNamespace(id="u-1", status="suspended"))) is None
