"""
Admin model switching (primary + ordered backups) through the real endpoints and a real app_settings table.

models.list is stubbed (no network): a fixed list stands in for Gemini's.
"""
import pytest

from app.common import llm_config
from app.common.app_settings import AppSetting

# Captured at import, before the session-wide no_model_settings_reads fixture
# replaces it: inside admin_client `engine` is the test's connection, so the
# real read is safe here.
_REAL_READ_ROWS = llm_config._read_rows

_LIST = [
    {"name": m, "spec": f"google_genai:{m}", "display_name": m, "input_token_limit": 1,
     "output_token_limit": 1, "thinking": None}
    for m in ("gemma-4-31b-it", "gemma-4-26b-it", "gemini-3.5-flash")
]


@pytest.fixture
def llm_settings(admin_client, monkeypatch):
    monkeypatch.setattr(llm_config, "_read_rows", _REAL_READ_ROWS)
    monkeypatch.setattr(llm_config, "_cache", {})
    monkeypatch.setattr(llm_config, "model_list", lambda refresh=False: (_LIST, 0.0))
    return admin_client


def _use(body, use):
    return next(u for u in body["uses"] if u["use"] == use)


def test_put_switches_the_model_without_a_restart(llm_settings):
    r = llm_settings.put("/api/v2/admin/settings/llm/agent", json={"primary": "gemma-4-26b-it"})
    assert r.status_code == 200, r.text
    assert r.json()["primary"] == {
        "value": "google_genai:gemma-4-26b-it", "model": "gemma-4-26b-it", "source": "app_settings",
        "env_default": "google_genai:gemma-4-31b-it", "stored": "google_genai:gemma-4-26b-it",
        "error": None, "in_model_list": True,
    }
    # The env backup list is [26b], now the primary, so it is skipped: no fallback.
    assert r.json()["backups"] == [] and r.json()["fallback_enabled"] is False
    # What the next agent turn resolves. (Building the model needs the API key,
    # which this tier deliberately leaves out of settings; the build from a
    # changed setting is covered in tests/unit/test_llm_fallback.py.)
    assert llm_config.resolve_models("agent")[0] == "google_genai:gemma-4-26b-it"

    r = llm_settings.put("/api/v2/admin/settings/llm/agent", json={"primary": None})
    assert _use(llm_settings.get("/api/v2/admin/settings/llm").json(), "agent")["primary"]["source"] == "env"


def test_put_sets_resets_and_clears_an_ordered_backup_list(llm_settings):
    url = "/api/v2/admin/settings/llm/recommendation"

    r = llm_settings.put(url, json={"backups": ["gemma-4-26b-it", "google_genai:gemma-4-31b-it"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(b["position"], b["value"], b["in_model_list"]) for b in body["backups"]] == [
        (1, "google_genai:gemma-4-26b-it", True), (2, "google_genai:gemma-4-31b-it", True),
    ]
    assert (body["backups_source"], body["max_backups"], body["fallback_enabled"]) == ("app_settings", 2, True)
    assert llm_config.resolve_models("recommendation")[1] == (
        "google_genai:gemma-4-26b-it", "google_genai:gemma-4-31b-it",
    )

    body = llm_settings.put(url, json={"backups": []}).json()  # no fallback
    assert (body["backups"], body["backups_stored"], body["fallback_enabled"]) == ([], "[]", False)

    body = llm_settings.put(url, json={"backups": None}).json()  # back to env
    assert body["backups_source"] == "env"
    assert [b["value"] for b in body["backups"]] == body["backups_env_default"]


@pytest.mark.parametrize("use, body, message", [
    ("agent", {"primary": "openai:gpt-5"}, "Unsupported provider"),
    ("agent", {"backups": ["google_genai:no-such-model"]}, "not in Gemini's models.list"),
    ("agent", {"backups": ["gemma-4-26b-it", "gemini-3.5-flash", "gemini-x-1"]}, "at most 2 backups"),
    ("recommendation", {"backups": ["gemma-4-26b-it", "gemma-4-31b-it", "gemini-x-1"]}, "at most 2 backups"),
    ("recommendation", {"backups": ["gemma-4-26b-it", "google_genai:gemma-4-26b-it"]}, "listed twice"),
    ("recommendation", {"backups": ["gemini-3.5-flash"]}, "is the primary"),
    ("recommendation", {"primary": "gemma-4-26b-it", "backups": ["gemma-4-26b-it"]}, "is the primary"),
    # The processor's limiter has no budget for flash, as primary or backup.
    ("processor", {"primary": "gemini-3.5-flash"}, "budget"),
    ("processor", {"backups": ["gemini-3.5-flash"]}, "budget"),
])
def test_put_rejects_what_cannot_work(llm_settings, use, body, message):
    r = llm_settings.put(f"/api/v2/admin/settings/llm/{use}", json=body)
    assert r.status_code == 400, r.text
    assert message in r.json()["detail"]
    # Nothing half-applied: every slot of the use is still the env default.
    u = _use(llm_settings.get("/api/v2/admin/settings/llm").json(), use)
    assert (u["primary"]["source"], u["backups_source"]) == ("env", "env")


def test_a_primary_that_collides_with_the_stored_backups_is_refused(llm_settings):
    url = "/api/v2/admin/settings/llm/recommendation"
    assert llm_settings.put(url, json={"backups": ["gemma-4-26b-it"]}).status_code == 200

    r = llm_settings.put(url, json={"primary": "gemma-4-26b-it"})
    assert r.status_code == 400 and "Send `backups`" in r.json()["detail"]

    # Both in one request is fine.
    r = llm_settings.put(url, json={"primary": "gemma-4-26b-it", "backups": ["gemma-4-31b-it"]})
    assert r.status_code == 200, r.text
    assert llm_settings.put("/api/v2/admin/settings/llm/nope", json={}).status_code == 404


def test_an_invalid_stored_list_is_surfaced_and_ignored(llm_settings, session):
    session.add(AppSetting(key="recommendation.backup_models", value='["google_genai:Not Valid"]'))
    session.add(AppSetting(key="review_processor.model", value="google_genai:gemini-0-retired"))
    session.commit()

    body = llm_settings.get("/api/v2/admin/settings/llm").json()

    rec = _use(body, "recommendation")
    assert [b["value"] for b in rec["backups"]] == rec["backups_env_default"]
    assert rec["backups_stored"] == '["google_genai:Not Valid"]' and "Malformed" in rec["backups_error"]
    # Well-formed but gone from models.list: in effect, and flagged.
    retired = _use(body, "review_processor")["primary"]
    assert (retired["source"], retired["in_model_list"]) == ("app_settings", False)
    assert any("Malformed" in w for w in body["warnings"])
    assert any("gemini-0-retired" in w for w in body["warnings"])


def test_models_endpoint_shows_positions_and_flags_missing_models(llm_settings, session):
    session.add(AppSetting(key="agent.backup_models",
                           value='["google_genai:gemma-4-26b-it", "google_genai:gemini-0-retired"]'))
    session.commit()

    body = llm_settings.get("/api/v2/admin/settings/llm/models").json()

    assert [m["name"] for m in body["models"]] == ["gemma-4-31b-it", "gemma-4-26b-it", "gemini-3.5-flash"]
    assert "agent.primary" in body["models"][0]["configured_for"]
    assert "agent.backup[1]" in body["models"][1]["configured_for"]
    assert "gemini-0-retired (agent.backup[2])" in body["configured_missing"]
    assert "no rate limits" in body["note"]


def test_legacy_processor_endpoint_shares_the_row_and_shows_the_full_chain(llm_settings):
    r = llm_settings.get("/api/v2/admin/settings/processor-model")
    assert r.json()["fallback_chain"] == [
        "gemma-4-31b-it", "gemma-4-26b-it", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    ]
    r = llm_settings.put("/api/v2/admin/settings/processor-model", json={"model": "gemma-4-26b-it"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "gemma-4-26b-it"
    # The new primary is skipped in the env backup list rather than tried twice.
    assert r.json()["fallback_chain"] == ["gemma-4-26b-it", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert _use(llm_settings.get("/api/v2/admin/settings/llm").json(), "processor")["primary"]["model"] == "gemma-4-26b-it"


def test_settings_endpoints_are_admin_only(api_client):
    assert api_client.get("/api/v2/admin/settings/llm").status_code == 403
    assert api_client.get("/api/v2/admin/settings/llm/models").status_code == 403
