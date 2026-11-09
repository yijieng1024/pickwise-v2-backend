"""retire_user_profile_fields: which users get snapshotted and cleared, and the counts."""
from tests.unit._adapters import profile_fields_plan

ROWS = [
    {"id": 1, "birthday": "2000-01-01", "gender": None, "occupation": None},
    {"id": 2, "birthday": None, "gender": None, "occupation": "Engineer"},
    {"id": 3, "birthday": None, "gender": None, "occupation": None},
    {"id": 4, "birthday": None, "gender": "Other", "occupation": "Student"},
]


def test_counts_each_field_and_selects_users_with_any_value():
    result = profile_fields_plan(ROWS)
    assert result["users"] == 4
    assert result["counts"] == {"birthday": 1, "gender": 1, "occupation": 2}
    assert [r["id"] for r in result["affected"]] == [1, 2, 4]


def test_nothing_to_do_once_cleared():
    empty = [{**r, "birthday": None, "gender": None, "occupation": None} for r in ROWS]
    assert profile_fields_plan(empty)["affected"] == []
