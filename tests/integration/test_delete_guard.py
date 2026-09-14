"""
DELETE /laptops/{id} must refuse while user- or pipeline-owned rows point at it.

Every FK to laptops.id is NO ACTION — there is no ON DELETE CASCADE anywhere in
the schema — so without the guard these five produce a raw IntegrityError 500.
That is why this belongs in the integration tier and not the unit one: the thing
being tested is a database constraint the guard exists to get in front of.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.laptops import laptop_router
from app.rag.models import Conversation, ConversationLaptop
from app.saved.models import SavedLaptop
from app.users.models import User
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


@pytest.fixture
def user(session):
    row = User(username="u", email="u@example.invalid", hashed_password="x")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture
def laptop(session, brand):
    row = make_laptop(brand.id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_an_unreferenced_laptop_deletes(session, laptop):
    """The guard must not be a blanket refusal — a row nothing points at is
    genuinely deletable."""
    laptop_router.delete_laptop(laptop.id, session)
    assert session.get(type(laptop), laptop.id) is None


def test_a_wishlisted_laptop_is_refused(session, laptop, user):
    session.add(SavedLaptop(user_id=user.id, laptop_id=laptop.id))
    session.commit()

    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(laptop.id, session)
    assert exc.value.status_code == 409
    assert "wishlist" in exc.value.detail


def test_the_message_tallies_each_referencing_table(session, laptop, user):
    """
    A refusal that says only "still referenced" sends an admin hunting. The
    count per table is the difference between a dead end and a next step.
    """
    session.add(SavedLaptop(user_id=user.id, laptop_id=laptop.id))
    conv = Conversation(user_id=user.id, title="t")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    session.add(
        ConversationLaptop(conversation_id=conv.id, laptop_id=laptop.id, similarity_score=0.9)
    )
    session.commit()

    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(laptop.id, session)
    detail = exc.value.detail
    assert "1 user wishlist" in detail
    assert "1 conversation shortlist" in detail


def test_a_missing_laptop_is_404_not_409(session):
    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(uuid.uuid4(), session)
    assert exc.value.status_code == 404


def test_the_message_names_the_retire_state_from_adr_0009(session, laptop, user):
    """
    ADR-0009 makes `suspended` the retired-and-no-longer-sold archive and
    `inactive` the awaiting-a-price work queue. The refusal used to point at
    `inactive`, which files a discontinued machine into the list of machines to
    go find a price for -- the exact conflation the three-value column exists to
    prevent. The enum value was corrected in August; this message was missed,
    and so was the line in CLAUDE.md that repeats it.

    The assertion is unchanged from when it was an xfail.
    """
    session.add(SavedLaptop(user_id=user.id, laptop_id=laptop.id))
    session.commit()

    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(laptop.id, session)
    assert "suspended" in exc.value.detail
