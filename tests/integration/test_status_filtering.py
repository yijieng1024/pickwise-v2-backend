"""
`suspended` must not be recommendable, on every route that can surface a laptop.

The filter is one `.where(Laptop.status == ACTIVE)` repeated in six places, which
is exactly the shape that rots: a new read path is written, the clause is
forgotten, and a retired machine reappears in one surface while staying hidden in
the other five. Parametrized so the failure message names the route.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
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


def test_a_laptop_cannot_have_an_unknown_weight(session, brand):
    """
    The NULL question, answered by the schema rather than by the filter.

    `weight_kg` is NOT NULL on `laptops`, so the "row with no weight" case the
    filter's NULL semantics would decide cannot occur at all. Worth an explicit
    test because the reasoning is invisible otherwise: SQL would drop such a row
    (`NULL <= 1.0` is NULL, not true), which is the behaviour we would want —
    but nothing depends on that, because the row cannot exist. If the column is
    ever made nullable this goes red, and the decision becomes live.
    """
    from tests.integration.conftest import make_laptop

    unknown = make_laptop(brand.id, product_name="Unknown", weight_kg=None)
    session.add(unknown)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_the_weight_filter_is_conditional(session, brand):
    """The filter must only apply when a limit is stated. A stated limit
    excludes; no limit at all must not quietly exclude the same rows."""
    from tests.integration.conftest import make_laptop

    session.add(make_laptop(brand.id, product_name="Light", weight_kg=0.9))
    session.add(make_laptop(brand.id, product_name="Heavy", weight_kg=2.5))
    session.commit()

    rows = _retrieval._relational_fallback(session, None, None, 50)
    assert {c.laptop.product_name for c in rows} == {"Light", "Heavy"}


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


# --------------------------------------------------------------------------
# A withheld score has no place in a ranking (ADR-0016)
# --------------------------------------------------------------------------


def test_ranking_omits_a_withheld_score(session, brand):
    """
    Omitted from the ORDERING, not deleted: the row still exists and
    GET /{id}/pick-scores still returns it with a null score and the flag. A
    ranking answers "what is best", and a laptop nobody could score has no
    answer to that question -- but the detail view still has to be able to say
    WHY there is no answer.

    Also guards a crash: get_ranking_for_use_case sorts on -score, which raises
    TypeError the moment a None reaches it.
    """
    from tests.integration.conftest import make_laptop

    scored = make_laptop(brand.id, product_name="Scored")
    withheld = make_laptop(brand.id, product_name="Withheld")
    session.add(scored)
    session.add(withheld)
    session.commit()

    session.add(LaptopPickScore(
        laptop_id=scored.id, use_case="gaming", score=80, breakdown=[], flags={}
    ))
    session.add(LaptopPickScore(
        laptop_id=withheld.id, use_case="gaming", score=None, breakdown=[],
        flags={"score_withheld": True},
    ))
    session.commit()

    rows = get_ranking_for_use_case(session, "gaming", limit=10)
    names = {laptop.product_name for _score, laptop, _brand in rows}
    assert names == {"Scored"}

    # The row itself survives -- absence must be explicit, not absent.
    stored = session.exec(
        LaptopPickScore.__table__.select().where(
            LaptopPickScore.laptop_id == withheld.id
        )
    ).all()
    assert len(stored) == 1
    assert stored[0].score is None
    assert stored[0].flags["score_withheld"] is True
