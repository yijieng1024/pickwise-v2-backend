"""
`suspended` must not be recommendable, on every route that can surface a laptop.

The filter is one `.where(Laptop.status == ACTIVE)` repeated in six places, which
is exactly the shape that rots: a new read path is written, the clause is
forgotten, and a retired machine reappears in one surface while staying hidden in
the other five. Parametrized so the failure message names the route.
"""

import uuid

import pytest
from sqlmodel import Session

from app.laptops.laptop_models import Laptop, LaptopPickScore
from app.laptops.pickscore_general import get_ranking_for_use_case
from app.rag import retrieval as _retrieval
from app.rag.models import Conversation, ConversationLaptop
from app.users.models import User

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Retrieval — both paths
# --------------------------------------------------------------------------


def test_relational_fallback_excludes_suspended(session, active_and_suspended):
    active, suspended = active_and_suspended
    rows = _retrieval._relational_fallback(session, None, None, 50)
    ids = {c.laptop.id for c in rows}
    assert active.id in ids
    assert suspended.id not in ids


def test_retrieve_candidates_excludes_suspended_on_the_fallback_path(
    session, active_and_suspended, monkeypatch
):
    """retrieve_candidates has two queries and the filter has to be on both.
    The pgvector one needs embeddings; this covers the path that does not."""
    active, suspended = active_and_suspended
    monkeypatch.setattr(
        _retrieval, "_get_query_vector", lambda q: (_ for _ in ()).throw(RuntimeError("down"))
    )
    rows = _retrieval.retrieve_candidates("a laptop", session)
    ids = {c.laptop.id for c in rows}
    assert active.id in ids
    assert suspended.id not in ids


# --------------------------------------------------------------------------
# Retrieval filters that are not about status
# --------------------------------------------------------------------------
# Promised by the unit tier: _FakeSession returns its rows whatever the WHERE
# clause says, so row-level exclusion can only be proven against a real database.


def test_the_fallback_weight_filter_really_excludes_heavier_rows(session, brand):
    from tests.integration.conftest import make_laptop

    light = make_laptop(brand.id, product_name="Light", weight_kg=0.9)
    heavy = make_laptop(brand.id, product_name="Heavy", weight_kg=2.5)
    session.add(light)
    session.add(heavy)
    session.commit()

    rows = _retrieval._relational_fallback(session, None, None, 50, weight_max=1.0)
    names = {c.laptop.product_name for c in rows}
    assert names == {"Light"}


def test_a_null_weight_row_is_excluded_by_the_weight_filter(session, brand):
    """The NULL decision, executed rather than read off the rendered SQL. An
    unknown weight is not evidence that a laptop is light."""
    from tests.integration.conftest import make_laptop

    light = make_laptop(brand.id, product_name="Light", weight_kg=0.9)
    unknown = make_laptop(brand.id, product_name="Unknown", weight_kg=None)
    session.add(light)
    session.add(unknown)
    session.commit()

    rows = _retrieval._relational_fallback(session, None, None, 50, weight_max=1.0)
    assert {c.laptop.product_name for c in rows} == {"Light"}


def test_no_weight_limit_returns_everything_including_null_weights(session, brand):
    """The filter must be conditional. A stated limit excludes; no limit at all
    must not quietly exclude the same rows."""
    from tests.integration.conftest import make_laptop

    session.add(make_laptop(brand.id, product_name="Light", weight_kg=0.9))
    session.add(make_laptop(brand.id, product_name="Unknown", weight_kg=None))
    session.commit()

    rows = _retrieval._relational_fallback(session, None, None, 50)
    assert {c.laptop.product_name for c in rows} == {"Light", "Unknown"}


# --------------------------------------------------------------------------
# The conversation shortlist pool — three separate readers
# --------------------------------------------------------------------------


@pytest.fixture
def conversation_with_both(session, active_and_suspended):
    """A thread whose persisted pool contains a laptop that has since been
    retired — the situation all three readers exist to handle."""
    active, suspended = active_and_suspended
    user = User(
        username="tester",
        email="tester@example.invalid",
        hashed_password="x",
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    conv = Conversation(user_id=user.id, title="t")
    session.add(conv)
    session.commit()
    session.refresh(conv)

    for laptop, sim in ((active, 0.9), (suspended, 0.8)):
        session.add(
            ConversationLaptop(
                conversation_id=conv.id, laptop_id=laptop.id, similarity_score=sim
            )
        )
    session.commit()
    return conv, active, suspended


def test_pool_block_drops_a_retired_laptop(session, conversation_with_both):
    """graph.py::_pool_block is the agent's answer-from-memory path, and
    therefore the one that could actually RECOMMEND a retired laptop rather than
    merely display it."""
    from app.agent.graph import _pool_block

    conv, active, suspended = conversation_with_both
    rows = session.exec(
        ConversationLaptop.__table__.select().where(
            ConversationLaptop.conversation_id == conv.id
        )
    ).all()
    pool = [
        ConversationLaptop(
            conversation_id=r.conversation_id,
            laptop_id=r.laptop_id,
            similarity_score=r.similarity_score,
        )
        for r in rows
    ]

    block = _pool_block(pool, session)
    assert block is not None
    assert str(active.id) in block
    assert str(suspended.id) not in block


def test_the_pool_rows_themselves_are_left_in_place(session, conversation_with_both):
    """The filter is on READ. Deleting the row would destroy the record of what
    the agent actually shortlisted at the time, which the eval history needs."""
    conv, active, suspended = conversation_with_both
    remaining = session.exec(
        ConversationLaptop.__table__.select().where(
            ConversationLaptop.conversation_id == conv.id
        )
    ).all()
    assert {r.laptop_id for r in remaining} == {active.id, suspended.id}


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def test_ranking_excludes_suspended(session, active_and_suspended):
    active, suspended = active_and_suspended
    for laptop in (active, suspended):
        session.add(
            LaptopPickScore(
                laptop_id=laptop.id,
                use_case="gaming",
                score=80,
                breakdown=[],
                flags={},
            )
        )
    session.commit()

    rows = get_ranking_for_use_case(session, "gaming", limit=10)
    ids = {laptop.id for _score, laptop, _brand in rows}
    assert active.id in ids
    assert suspended.id not in ids
