"""admin_note is for admins only: no public questionnaire response may carry it,
and the route that does carry it requires an admin."""
import pytest

from tests.unit._adapters import (
    get_current_admin_dependency, questionnaire_help_plan, questionnaire_help_refused,
    questionnaire_new_help, questionnaire_routes,
)

PUBLIC = [("GET", "/api/v2/questionnaire"), ("GET", "/api/v2/questionnaire/{question_id}")]


def test_public_reads_never_carry_admin_note():
    routes = questionnaire_routes()
    for key in PUBLIC:
        fields, _ = routes[key]
        assert "help_text" in fields, f"sanity: {key} response model was read"
        assert "admin_note" not in fields, f"{key} leaks admin_note"


def test_every_route_that_returns_admin_note_requires_an_admin():
    admin = get_current_admin_dependency()
    carrying = {k: deps for k, (fields, deps) in questionnaire_routes().items() if "admin_note" in fields}
    assert ("GET", "/api/v2/questionnaire/admin") in carrying
    assert all(admin in deps for deps in carrying.values()), carrying.keys()


def _rows(**override):
    return [{"id": i, "step_order": i, "target_field": f, "question_text": "q",
             "help_text": override.get(f, f"To know user's {f}"), "admin_note": None}
            for i, f in enumerate(questionnaire_new_help(), 1)]


def test_plan_moves_the_internal_note_and_writes_user_text():
    entries = questionnaire_help_plan(_rows())
    budget = next(e for e in entries if e["target_field"] == "budget")
    assert budget["admin_note"] == "To know user's budget"
    assert budget["help_text_new"] == questionnaire_new_help()["budget"]


def test_plan_refuses_a_second_run():
    done = [{**r, "admin_note": r["help_text"], "help_text": "Pick the range"} for r in _rows()]
    with pytest.raises(questionnaire_help_refused()):
        questionnaire_help_plan(done)
