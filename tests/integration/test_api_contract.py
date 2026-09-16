"""
Tier 4 -- the API contract.

The seam between the engine and its callers: the SHAPE of what comes out of an
endpoint, as opposed to whether the number inside it is right. Tiers 0 and 2
own the numbers. This file owns the contract.

Runs through FastAPI's TestClient against the real app, inside each test's
rollback transaction. See `_app_client` in conftest.py for what is redirected
and, precisely, what the auth override does and does not bypass -- the first
group below exists to prove the "does not" half.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.benchmark.model import CPUBenchmark
from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop, LaptopPickScore
from app.rag.models import Conversation, Message
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# The override bypasses authentication, not authorization
# --------------------------------------------------------------------------


def test_the_override_does_not_grant_admin(api_client, brand, session):
    """
    Catches a test fixture that disables more than it claims. api_client is
    authenticated as a NON-admin, and get_current_admin depends on
    get_current_user -- so the role check still runs against that user and
    still refuses. If this ever returns 200, every admin-route test in the tier
    is passing for the wrong reason.
    """
    laptop = make_laptop(brand.id)
    session.add(laptop)
    session.commit()

    r = api_client.put(f"/api/v2/laptops/{laptop.id}", json={"product_name": "x"})
    assert r.status_code == 403


def test_the_override_does_not_bypass_ownership(api_client, session):
    """Per-resource authorization survives too: a conversation that belongs to
    someone else is still a 404 through the overridden identity."""
    from app.users.models import User

    stranger = User(
        username=f"s-{uuid.uuid4().hex[:8]}",
        email=f"s-{uuid.uuid4().hex[:8]}@example.invalid",
        hashed_password="x",
        status="active",
    )
    session.add(stranger)
    session.commit()
    theirs = Conversation(user_id=stranger.id, title="not yours")
    session.add(theirs)
    session.commit()

    r = api_client.get(f"/api/v2/conversations/{theirs.id}/laptops")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# The withheld-score contract (ADR-0016)
# --------------------------------------------------------------------------


@pytest.fixture
def withheld_laptop(session, brand):
    """A laptop whose stored score is withheld: row present, score NULL, flag
    set -- exactly what generate_all_pick_scores writes when both defining
    factors are unresolved."""
    laptop = make_laptop(brand.id, product_name="Unidentifiable")
    session.add(laptop)
    session.commit()
    session.add(LaptopPickScore(
        laptop_id=laptop.id,
        use_case="gaming",
        score=None,
        breakdown=[{"factor": "price", "raw_score": 60.0, "weight": 0.2,
                    "contribution": 12.0, "note": None}],
        flags={
            "score_withheld": True,
            "cpu_benchmark_unresolved": True,
            "gpu_benchmark_unresolved": True,
            "gpu_score_is_proxy": True,
            "price_unavailable": False,
        },
    ))
    session.commit()
    return laptop


@pytest.mark.xfail(
    strict=True,
    reason=(
        "LIVE BUG, reported not fixed. ADR-0016 stage 2 made laptop_pick_scores."
        "score nullable and PickScoreResponse.score Optional, but missed the "
        "ROUTER's own response model: app/laptops/pickscore_router.py "
        "UseCasePickScore.score is still `int`. GET /{laptop_id}/pick-scores on a "
        "withheld row therefore fails pydantic validation inside the route and "
        "returns a server error instead of the null score and breakdown ADR-0016 "
        "promises. The stage-2 commit message claimed this route 'returns it "
        "unchanged: null score, flag set, full breakdown' -- it never did."
    ),
)
def test_a_withheld_score_is_served_as_null_with_its_breakdown(api_client, withheld_laptop):
    """
    The contract: the row comes back, the total is null -- NEVER 0, which means
    "scored, and badly" -- and the breakdown survives, because withholding the
    total must not withhold the evidence needed to explain why there is none.
    """
    r = api_client.get(f"/api/v2/laptops/{withheld_laptop.id}/pick-scores")
    assert r.status_code == 200, r.text
    (row,) = r.json()["scores"]
    assert row["score"] is None
    assert row["score"] != 0
    assert row["breakdown"], "withholding the total must not withhold the evidence"
    assert row["flags"]["score_withheld"] is True


def test_the_generator_writes_a_withheld_score_as_null(session, brand):
    """
    The real write path: generate_all_pick_scores over a laptop the engine
    cannot score. Every row must land with score NULL and the flag -- never 0.

    Needed separately from the agent test below, which is insulated from the
    engine's value: search_laptops checks flags.score_withheld and emits
    pick_score None itself, so an engine that returned 0 would sail straight
    past it. This is the test that sees the number the engine actually wrote.
    Read from the table rather than through GET /{id}/pick-scores, because that
    route currently cannot serve a withheld row at all (see the xfail above).
    """
    from app.laptops.pickscore_general import generate_all_pick_scores

    laptop = make_laptop(brand.id, processor_model="Unknown", gpu_model="Unknown")
    session.add(laptop)
    session.commit()

    generate_all_pick_scores(session)

    rows = session.exec(
        select(LaptopPickScore).where(LaptopPickScore.laptop_id == laptop.id)
    ).all()
    assert len(rows) == 5, "one row per use case -- a withheld score is written, not skipped"
    for row in rows:
        assert row.score is None, f"{row.use_case}: withheld score written as {row.score!r}"
        assert row.flags["score_withheld"] is True


def test_the_ranking_omits_a_withheld_score_and_only_the_ranking(
    api_client, withheld_laptop, brand, session
):
    """
    Omitted from the ORDERING, kept everywhere else: a ranking answers "what is
    best", and a laptop nobody could score has no position in it. The row itself
    must survive, or "withheld" becomes indistinguishable from "never generated".
    """
    scored = make_laptop(brand.id, product_name="Scored")
    session.add(scored)
    session.commit()
    session.add(LaptopPickScore(
        laptop_id=scored.id, use_case="gaming", score=70, breakdown=[], flags={}
    ))
    session.commit()

    r = api_client.get("/api/v2/laptops/pick-scores/ranking", params={"use_case": "gaming"})
    assert r.status_code == 200, r.text
    ranked = {uuid.UUID(x["laptop_id"]) for x in r.json()["results"]}
    assert scored.id in ranked
    assert withheld_laptop.id not in ranked

    kept = session.exec(
        select(LaptopPickScore).where(LaptopPickScore.laptop_id == withheld_laptop.id)
    ).all()
    assert len(kept) == 1 and kept[0].score is None


def test_the_agent_reports_why_a_score_is_unavailable(session, brand):
    """
    search_laptops must say WHY there is no badge, so the model relays it
    instead of silently dropping the score or inventing one. An absent key
    already means "scoring failed"; a withheld score is a different answer.

    Called at the tool's own seam rather than through /agent/chat, because
    reaching it through the HTTP route means a real model choosing to call the
    tool. With empty benchmark tables every CPU and GPU is unresolved, which is
    the both-unresolved condition that withholds.
    """
    from app.agent.tools import search_laptops as _tool_pkg  # noqa: F401
    import importlib

    tool = importlib.import_module("app.agent.tools.search_laptops")
    from app.rag.reranker import RankedCandidate

    laptop = make_laptop(brand.id, processor_model="Unknown", gpu_model="Unknown")
    session.add(laptop)
    session.commit()
    session.refresh(laptop)

    candidate = RankedCandidate(
        laptop_id=str(laptop.id), product_name="Unknown machine", price_rm=4000.0,
        similarity_score=0.8, penalty_multiplier=1.0, bonus=0.0, final_score=0.8,
        penalty_reasons=[], bonus_reasons=[], _laptop=laptop, _brand_name="Asus",
    )
    scores = tool._pick_scores_for([candidate], session, None)
    entry = scores[str(laptop.id)]

    assert entry["pick_score"] is None
    assert entry["pick_score"] != 0
    reason = entry.get("pick_score_unavailable")
    assert reason and reason.strip(), "withheld with no reason given to the model"


def test_the_flag_names_the_frontend_contract_names_are_what_the_api_emits(
    api_client, brand, session
):
    """
    ADR-0016 told the frontend to read these exact keys. If the API emits them
    under any other name the frontend renders nothing and nobody is told, so the
    names are asserted against a row the REAL engine generated -- not a row this
    test wrote by hand, which would only prove the test can spell.

    One benchmark resolves (the CPU) and one does not (the GPU), so the three
    flags have to be present AND take different values; identical values would
    not show they are distinguishable.
    """
    from app.laptops.pickscore_general import generate_all_pick_scores

    laptop = make_laptop(
        brand.id,
        processor_model="Intel Core i7-14650HX",
        gpu_model="Unknown",
    )
    session.add(laptop)
    session.add(CPUBenchmark(cpu_name="Intel Core i7-14650HX", cpu_mark=33467))
    session.commit()

    generate_all_pick_scores(session)

    r = api_client.get(f"/api/v2/laptops/{laptop.id}/pick-scores", params={"use_case": "gaming"})
    assert r.status_code == 200, r.text
    (row,) = r.json()["scores"]
    flags = row["flags"]

    for key in ("score_withheld", "cpu_benchmark_unresolved", "gpu_benchmark_unresolved"):
        assert key in flags, f"the API does not emit {key!r}, which the frontend was told to read"

    assert flags["cpu_benchmark_unresolved"] is False
    assert flags["gpu_benchmark_unresolved"] is True
    assert flags["score_withheld"] is False
    assert isinstance(row["score"], int)


# --------------------------------------------------------------------------
# Validation and writability
# --------------------------------------------------------------------------


def test_an_unknown_status_filter_is_rejected(api_client):
    """A typo'd filter must not silently mean 'no filter' -- that would show an
    admin every retired laptop while they believe they filtered them out."""
    r = api_client.get("/api/v2/laptops/", params={"status": "bogus"})
    assert r.status_code == 422


def test_omitting_status_returns_every_status(api_client, brand, session):
    """Omitted means ALL statuses, deliberately, so the admin catalog view keeps
    seeing retired rows. Scoped by a unique search term so rows other tests
    might add cannot make this pass or fail."""
    tag = uuid.uuid4().hex[:10]
    for status in ("active", "inactive", "suspended"):
        session.add(make_laptop(brand.id, product_name=f"{tag} {status}", status=status))
    session.commit()

    r = api_client.get("/api/v2/laptops/", params={"search": tag})
    assert r.status_code == 200, r.text
    assert {x["status"] for x in r.json()} == {"active", "inactive", "suspended"}


def test_family_id_is_writable_through_families(admin_client, brand, session):
    """The one sanctioned path. Moving through /families is what keeps a merge
    all-or-nothing and reports the emptied families."""
    laptop = make_laptop(brand.id)
    family = LaptopFamily(brand_id=brand.id, name="Some Family", family_key="some family")
    session.add(laptop)
    session.add(family)
    session.commit()

    r = admin_client.post(
        "/api/v2/families/laptops/move",
        json={"laptop_ids": [str(laptop.id)], "target_family_id": str(family.id)},
    )
    assert r.status_code == 200, r.text
    session.refresh(laptop)
    assert laptop.family_id == family.id


def test_family_id_cannot_be_changed_through_put_laptops(admin_client, brand, session):
    """
    The invariant that matters: PUT /laptops/{id} does NOT move a laptop between
    families. The processor's upsert relies on the same property -- re-processing
    a page must never undo a placement an admin made by hand.
    """
    laptop = make_laptop(brand.id)
    family = LaptopFamily(brand_id=brand.id, name="Target", family_key="target")
    session.add(laptop)
    session.add(family)
    session.commit()

    admin_client.put(
        f"/api/v2/laptops/{laptop.id}", json={"family_id": str(family.id)}
    )
    session.refresh(laptop)
    assert laptop.family_id is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING, reported not fixed. PUT /laptops/{id} with family_id returns 200 "
        "and silently IGNORES the field: LaptopUpdate has no family_id and does not "
        "set extra='forbid', so pydantic drops unknown keys. The write is refused, "
        "which protects the invariant, but a caller is told it succeeded. That is "
        "worse than rejecting it -- an admin screen would show a successful save of "
        "a change that never happened."
    ),
)
def test_family_id_through_put_laptops_is_rejected_not_ignored(admin_client, brand, session):
    laptop = make_laptop(brand.id)
    family = LaptopFamily(brand_id=brand.id, name="Target", family_key="target")
    session.add(laptop)
    session.add(family)
    session.commit()

    r = admin_client.put(f"/api/v2/laptops/{laptop.id}", json={"family_id": str(family.id)})
    assert 400 <= r.status_code < 500


def test_a_non_active_laptop_still_serves_its_stale_scores(api_client, brand, session):
    """
    RECORDED, NOT ENDORSED. A suspended laptop keeps the PickScore rows it had,
    computed against catalog ranges that no longer include it, and the endpoint
    serves them with nothing saying so: no staleness flag, no status, no
    computed-against timestamp beyond updated_at.

    generate_all_pick_scores only regenerates ACTIVE laptops, so these rows
    never move again. This test pins the behaviour so the gap stays visible --
    if a staleness signal is ever added, this is the test to update.
    """
    laptop = make_laptop(brand.id, status="suspended")
    session.add(laptop)
    session.commit()
    session.add(LaptopPickScore(
        laptop_id=laptop.id, use_case="gaming", score=64, breakdown=[], flags={}
    ))
    session.commit()

    r = api_client.get(f"/api/v2/laptops/{laptop.id}/pick-scores")
    assert r.status_code == 200, r.text
    (row,) = r.json()["scores"]
    assert row["score"] == 64
    assert not any("stale" in k for k in row["flags"]), "a staleness signal now exists -- update this test"


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_a_route_that_opens_its_own_session_still_commits_nothing(
    api_client, api_user, session, engine, monkeypatch
):
    """
    TestClient's routes do not share the test's session when they build their
    own -- /agent/chat takes no session dependency and opens Session(engine)
    through session_scope(). If that engine were a real engine, its commit would
    be REAL and visible to every later test, and the data-invariant tests would
    start failing against this file's leftovers.

    Proved from OUTSIDE the transaction: after the request, a fresh connection
    from the tier's engine counts the rows. It must see none, while the test's
    own session -- inside the transaction -- must see them.
    """
    import app.agent.graph as graph

    async def _turn(*a, **k):
        return "hello", None

    monkeypatch.setattr(graph, "run_agent", _turn)
    marker = f"isolation-{uuid.uuid4().hex}"

    r = api_client.post("/api/v2/agent/chat", json={"message": marker})
    assert r.status_code == 200, r.text

    inside = session.exec(select(Message).where(Message.content == marker)).all()
    assert inside, "the route wrote nothing at all -- the test proves nothing"

    with engine.connect() as outside:
        leaked = outside.execute(
            text("SELECT count(*) FROM messages WHERE content = :m"), {"m": marker}
        ).scalar()
    assert leaked == 0, "the route COMMITTED -- it is not inside the rollback transaction"
