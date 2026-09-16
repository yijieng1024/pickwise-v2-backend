"""
`suspended` must not be recommendable, on every route that can surface a laptop.

The filter is one `.where(Laptop.status == ACTIVE)` repeated across six surfaces,
which is exactly the shape that rots: a new read path is written, the clause is
forgotten, and a retired machine reappears in one surface while staying hidden in
the others.

ONE parametrized test over ONE table of surfaces, so the failure names the
route and a seventh surface is added by appending a row, not by writing a
parallel test somewhere else. This file's docstring used to claim that
parametrization while the file actually held four separate functions -- and the
two conversation_laptops reads that needed Tier 4's HTTP apparatus had no
coverage anywhere, their guarantee resting on reading the code.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.laptops.laptop_models import Laptop, LaptopPickScore
from app.laptops.pickscore_general import get_ranking_for_use_case
from app.rag import retrieval as _retrieval
from app.rag.models import Conversation, ConversationLaptop
from app.users.models import User

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Retrieval — both paths
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Every surface, one test
# --------------------------------------------------------------------------


@pytest.fixture
def surfaced(session, api_client, api_user, active_and_suspended, monkeypatch):
    """
    One seeded world every surface reads from: an active and a suspended laptop,
    both already in a conversation's shortlist pool owned by the API user, and
    both carrying a stored gaming PickScore. Each surface then answers the same
    question -- which laptop ids do you expose? -- so the assertion is identical
    across all of them and the only thing that varies is the route.
    """
    active, suspended = active_and_suspended

    conv = Conversation(user_id=api_user.id, title="t")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    for laptop, sim in ((active, 0.9), (suspended, 0.8)):
        session.add(ConversationLaptop(
            conversation_id=conv.id, laptop_id=laptop.id, similarity_score=sim
        ))
        session.add(LaptopPickScore(
            laptop_id=laptop.id, use_case="gaming", score=70, breakdown=[], flags={}
        ))
    session.commit()

    return {
        "session": session,
        "client": api_client,
        "conversation": conv,
        "active": active,
        "suspended": suspended,
        "monkeypatch": monkeypatch,
    }


def _via_relational_fallback(w):
    return {c.laptop.id for c in _retrieval._relational_fallback(w["session"], None, None, 50)}


def _via_retrieve_candidates(w):
    """retrieve_candidates has two queries and the filter has to be on both.
    The pgvector one needs embeddings; forcing the embedding call to fail sends
    it down the relational path, which is the one reachable here."""
    w["monkeypatch"].setattr(
        _retrieval, "_get_query_vector",
        lambda q: (_ for _ in ()).throw(RuntimeError("down")),
    )
    return {c.laptop.id for c in _retrieval.retrieve_candidates("a laptop", w["session"])}


def _via_pool_block(w):
    """graph.py::_pool_block -- the agent's answer-from-memory path, and
    therefore the one that could RECOMMEND a retired laptop, not merely show it."""
    from app.agent.graph import _pool_block

    pool = w["session"].exec(
        select(ConversationLaptop).where(
            ConversationLaptop.conversation_id == w["conversation"].id
        )
    ).all()
    block = _pool_block(list(pool), w["session"]) or ""
    return {lid for lid in (w["active"].id, w["suspended"].id) if str(lid) in block}


def _via_ranking(w):
    rows = get_ranking_for_use_case(w["session"], "gaming", limit=10)
    return {laptop.id for _score, laptop, _brand in rows}


def _via_get_conversation_laptops(w):
    """app/rag/router.py -- GET /conversations/{id}/laptops, the endpoint the
    frontend calls to restore the shortlist rail when a conversation reopens."""
    r = w["client"].get(f"/api/v2/conversations/{w['conversation'].id}/laptops")
    assert r.status_code == 200, r.text
    return {uuid.UUID(card["laptop_id"]) for card in r.json()}


def _via_agent_chat_pool(w):
    """
    app/agent/router.py -- the persisted-pool read inside _persist_assistant_turn,
    taken on any turn where search_laptops did not run. Reached through the real
    POST /agent/chat with the LLM turn stubbed: the read is what is under test,
    not the model, and a real Gemini call would make this flaky and paid.
    """
    import app.agent.graph as graph

    async def _no_search_turn(*args, **kwargs):
        return "Here is what we looked at before.", None  # tool_results=None

    w["monkeypatch"].setattr(graph, "run_agent", _no_search_turn)
    r = w["client"].post(
        "/api/v2/agent/chat",
        json={"message": "what did we shortlist?", "conversation_id": str(w["conversation"].id)},
    )
    assert r.status_code == 200, r.text
    return {uuid.UUID(card["laptop_id"]) for card in r.json()["laptops"]}


_SURFACES = [
    pytest.param(_via_relational_fallback, id="rag.retrieval._relational_fallback"),
    pytest.param(_via_retrieve_candidates, id="rag.retrieval.retrieve_candidates"),
    pytest.param(_via_pool_block, id="agent.graph._pool_block"),
    pytest.param(_via_ranking, id="pickscore_general.get_ranking_for_use_case"),
    pytest.param(_via_get_conversation_laptops, id="GET /conversations/{id}/laptops"),
    pytest.param(_via_agent_chat_pool, id="POST /agent/chat (persisted pool)"),
]


@pytest.mark.parametrize("surface", _SURFACES)
def test_suspended_is_never_surfaced(surfaced, surface):
    """
    Catches a read path that forgot the status clause. Asserted two ways on
    purpose: the suspended laptop is absent, AND the active one is present --
    an empty result would satisfy the first alone, and a surface that returns
    nothing is broken, not filtered.
    """
    exposed = surface(surfaced)
    assert surfaced["active"].id in exposed, "the active laptop is missing -- surface returned nothing useful"
    assert surfaced["suspended"].id not in exposed, "a SUSPENDED laptop was surfaced"


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
