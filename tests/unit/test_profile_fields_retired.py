"""birthday, gender and occupation are retired: the profile endpoints neither accept
nor return them."""
import pytest
from pydantic import ValidationError

from tests.unit._adapters import profile_body, user_read_fields

RETIRED = ("birthday", "gender", "occupation")


@pytest.mark.parametrize("field,value", [("birthday", "2000-01-01"), ("gender", "Other"), ("occupation", "x")])
def test_sending_a_retired_field_is_rejected(field, value):
    with pytest.raises(ValidationError):
        profile_body({field: value})


def test_profile_response_no_longer_carries_them():
    fields = user_read_fields()
    assert "username" in fields
    assert not fields & set(RETIRED)
