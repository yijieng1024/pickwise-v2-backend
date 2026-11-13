"""GET and PUT /admin/settings/pipeline (plan step 11, item 8), on a real table."""
import pytest

from app.pipeline.settings import PipelineSettings

pytestmark = pytest.mark.integration

URL = "/api/v2/admin/settings/pipeline"


def test_get_returns_the_defaults_and_put_changes_only_what_is_sent(admin_client, session):
    r = admin_client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    del body["updated_at"]
    assert body == {"enabled": False, "weekday": "sunday", "report_email": "", "price_jump_percent": 30,
                    "asus_enabled": True, "rog_enabled": True, "hp_enabled": True}

    r = admin_client.put(URL, json={"enabled": True, "weekday": "Wednesday", "report_email": "jack@example.com",
                                    "rog_enabled": False})
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in ("enabled", "weekday", "report_email", "price_jump_percent", "rog_enabled")} == {
        "enabled": True, "weekday": "wednesday", "report_email": "jack@example.com", "price_jump_percent": 30,
        "rog_enabled": False}
    stored = session.get(PipelineSettings, 1)
    assert (stored.enabled, stored.weekday, stored.rog_enabled, stored.hp_enabled) == (True, "wednesday", False, True)

    r = admin_client.put(URL, json={"price_jump_percent": 45})
    assert (r.json()["price_jump_percent"], r.json()["weekday"]) == (45, "wednesday")


@pytest.mark.parametrize("body", [{"price_jump_percent": 0}, {"price_jump_percent": 101},
                                  {"report_email": "not-an-email"}, {"weekday": "someday"}])
def test_put_refuses_a_bad_value_and_writes_nothing(admin_client, session, body):
    r = admin_client.put(URL, json=body)
    assert r.status_code == 422, r.text
    assert session.get(PipelineSettings, 1) is None


def test_a_non_admin_is_refused(api_client):
    assert api_client.get(URL).status_code == 403
    assert api_client.put(URL, json={"enabled": True}).status_code == 403
