"""The wizard keeps its browser draft only when it is newer than the saved answers,
so a save must move updated_at, and the response must carry it."""
import uuid
from datetime import datetime, timezone

from tests.unit._adapters import preferences_read_fields, preferences_request_fields, preferences_update

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_a_save_moves_updated_at_and_keeps_unsent_fields():
    row = preferences_update({"user_id": uuid.uuid4(), "portability": "Yes", "updated_at": OLD},
                             {"tech_savviness": "Very tech-savvy"})
    assert row.updated_at > OLD
    assert (row.tech_savviness, row.portability) == ("Very tech-savvy", "Yes")


def test_the_response_carries_updated_at_but_a_client_cannot_send_it():
    assert "updated_at" in preferences_read_fields()
    assert "updated_at" not in preferences_request_fields()
