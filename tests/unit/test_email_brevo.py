"""
`app/users/email.py::_send` against the Brevo HTTP API.

Unit tier: `httpx.post` is replaced, so nothing here touches the network and
no API key is needed. That matters more than usual for this module -- a test
that reached the real endpoint would burn the 300/day free quota and mail a
stranger.
"""

import logging
import types

import httpx
import pytest

from tests.unit._adapters import email_module

email_module = email_module()

pytestmark = pytest.mark.unit

_FAKE_KEY = "xkeysib-unit-test-key-never-real"


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def brevo(monkeypatch):
    """Stub settings + httpx.post; hand back the recorded call."""
    monkeypatch.setattr(
        email_module,
        "settings",
        types.SimpleNamespace(
            brevo_api_key=_FAKE_KEY,
            email_sender_address="noreply@ngyijie.com",
            email_sender_name="PickWise",
            frontend_url="https://pickwise.ngyijie.com",
            email_verification_token_expire_hours=1,
        ),
    )

    calls = []
    result = {"response": _FakeResponse(201, '{"messageId":"<x@brevo>"}')}

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        response = result["response"]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(email_module.httpx, "post", fake_post)
    return types.SimpleNamespace(calls=calls, result=result)


def test_send_success_posts_expected_request(brevo):
    assert email_module._send("user@example.com", "Subj", "<p>hi</p>", "hi") is True

    assert len(brevo.calls) == 1
    call = brevo.calls[0]
    assert call["url"] == "https://api.brevo.com/v3/smtp/email"
    assert call["headers"]["api-key"] == _FAKE_KEY

    body = call["json"]
    assert body["to"] == [{"email": "user@example.com"}]
    assert body["sender"] == {"name": "PickWise", "email": "noreply@ngyijie.com"}
    assert body["subject"] == "Subj"
    # Both parts, every time -- an HTML-only message is a spam-filter magnet.
    assert body["htmlContent"] == "<p>hi</p>"
    assert body["textContent"] == "hi"


@pytest.mark.parametrize(
    "status_code, text",
    [
        (401, '{"code":"unauthorized","message":"Key not found"}'),
        (400, '{"code":"invalid_parameter","message":"sender not valid"}'),
        (500, "upstream boom"),
    ],
)
def test_send_returns_false_on_error_status(brevo, caplog, status_code, text):
    brevo.result["response"] = _FakeResponse(status_code, text)

    with caplog.at_level(logging.ERROR):
        assert email_module._send("user@example.com", "S", "<p>h</p>", "h") is False

    # The status and the body are both needed to tell a bad key from an
    # unverified sender, so both must reach the log.
    logged = caplog.text
    assert str(status_code) in logged
    assert text[:40] in logged


def test_send_returns_false_on_transport_error(brevo, caplog):
    brevo.result["response"] = httpx.ConnectTimeout("timed out")

    with caplog.at_level(logging.ERROR):
        assert email_module._send("user@example.com", "S", "<p>h</p>", "h") is False

    assert "timed out" in caplog.text


def test_api_key_never_reaches_the_log(brevo, caplog):
    """The key is in the header dict of every call -- it must never be logged,
    on the failure paths least of all."""
    for response in (
        _FakeResponse(401, '{"message":"Key not found"}'),
        httpx.ConnectError("no route"),
    ):
        caplog.clear()
        brevo.result["response"] = response
        with caplog.at_level(logging.DEBUG):
            email_module._send("user@example.com", "S", "<p>h</p>", "h")
        assert _FAKE_KEY not in caplog.text


def test_verification_email_links_to_the_frontend_page(brevo):
    """The link must reach the frontend /verify-email page (which can offer a
    resend on an expired token), not the backend's raw-JSON endpoint."""
    assert email_module.send_verification_email("user@example.com", "tok123") is True

    body = brevo.calls[0]["json"]
    expected = "https://pickwise.ngyijie.com/verify-email?token=tok123"
    assert expected in body["htmlContent"]
    assert expected in body["textContent"]


def test_reset_email_links_to_the_frontend_page(brevo):
    assert email_module.send_password_reset_email("user@example.com", "tok456") is True

    body = brevo.calls[0]["json"]
    expected = "https://pickwise.ngyijie.com/reset-password?token=tok456"
    assert expected in body["htmlContent"]
    assert expected in body["textContent"]


def test_google_notice_email_carries_no_token(brevo):
    assert email_module.send_google_account_notice_email("user@example.com") is True

    body = brevo.calls[0]["json"]
    assert "token=" not in body["htmlContent"]
    assert "https://pickwise.ngyijie.com/login" in body["textContent"]
