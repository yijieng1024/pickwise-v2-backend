"""Onboarding background answers (user_background): partial saves, the user_type
vocabulary, and field of study existing only for students."""
import uuid

import pytest
from pydantic import ValidationError

from tests.unit._adapters import background_update

ROW = dict(user_id=uuid.uuid4(), user_type="working", current_laptop="ThinkPad T480",
           change_reason="battery is dead")


def test_sending_one_field_leaves_the_others_unchanged():
    row = background_update(ROW, {"current_laptop": "MacBook Air M1"})
    assert row.current_laptop == "MacBook Air M1"
    assert (row.user_type, row.change_reason) == ("working", "battery is dead")


def test_explicit_null_clears_a_field():
    assert background_update(ROW, {"change_reason": None}).change_reason is None


@pytest.mark.parametrize("value", ["student", "working", "buying_for_business", "other"])
def test_known_user_types_are_accepted(value):
    assert background_update(ROW, {"user_type": value}).user_type == value


@pytest.mark.parametrize("value", ["gamer", "Student", ""])
def test_unknown_user_type_is_rejected(value):
    with pytest.raises(ValidationError, match="user_type"):
        background_update(ROW, {"user_type": value})


def test_leaving_student_clears_field_of_study():
    student = {**ROW, "user_type": "student", "field_of_study": "Computer Science"}
    assert background_update(student, {"current_laptop": "x"}).field_of_study == "Computer Science"
    assert background_update(student, {"user_type": "working"}).field_of_study is None


def test_saving_only_field_of_study_keeps_it_when_stored_type_is_student():
    # Per-question saves send one field at a time: the rule must read the stored
    # user_type merged with the body, not the body alone.
    student = {**ROW, "user_type": "student"}
    assert background_update(student, {"field_of_study": "Accounting"}).field_of_study == "Accounting"
