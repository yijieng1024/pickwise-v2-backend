"""
Review C: one call per window decides where its claim goes; a review whose
links change is re-processed into staging and swapped in only when complete;
the full re-run stages everything and swaps (and rolls back) in one
transaction; delete guards count staged and unattached claims; price claims
are hidden from the public endpoint and the agent.
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import select

from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

_VEC = [0.01] * 768


@pytest.fixture
def world(session, brand):
    from app.laptops.family_model import LaptopFamily

    def family(name):
        f = LaptopFamily(brand_id=brand.id, name=name, family_key=name.lower())
        session.add(f)
        session.commit()
        return f

    strix, zeph = family("Strix 16"), family("Zephyrus G16")
    # Strix: configurations differ by GPU, so they are listed (F1.C1, F1.C2).
    s1 = make_laptop(brand.id, product_name="Strix A", family_id=strix.id,
                     gpu_model="NVIDIA GeForce RTX 5060 Laptop GPU")
    s2 = make_laptop(brand.id, product_name="Strix B", family_id=strix.id,
                     gpu_model="NVIDIA GeForce RTX 5080 Laptop GPU")
    # Zephyrus: two identical rows, nothing to list.
    z1 = make_laptop(brand.id, product_name="Zephyrus A", family_id=zeph.id)
    z2 = make_laptop(brand.id, product_name="Zephyrus B", family_id=zeph.id)
    session.add_all([s1, s2, z1, z2])
    session.commit()
    return SimpleNamespace(strix=strix, zeph=zeph, s1=s1, s2=s2, z1=z1, z2=z2)


def _review(session, status="matched"):
    from app.reviews.models import RawYoutubeReview, YoutubeChannel

    ch = YoutubeChannel(channel_id=f"UC{uuid.uuid4().hex[:10]}", channel_name="Chan", active=True)
    session.add(ch)
    session.commit()
    row = RawYoutubeReview(
        video_id=f"v{uuid.uuid4().hex[:8]}", channel_id=ch.channel_id,
        video_title="Strix vs Zephyrus", status=status,
        # 12 x 15 s segments -> four 45-second windows (parts 0, 3, 6, 9 open them).
        raw_transcript={"segments": [
            {"start": float(i * 15), "duration": 15.0, "text": f"part {i}"} for i in range(12)
        ]},
    )
    session.add(row)
    session.commit()
    return row


def _link(session, review, family, laptop=None):
    from app.reviews.link_model import ReviewLaptopLink

    link = ReviewLaptopLink(raw_review_id=review.id, family_id=family.id,
                            laptop_id=laptop.id if laptop else None, match_source="human")
    session.add(link)
    session.commit()
    return link


@pytest.fixture
def model(monkeypatch):
    """The model's answer per window, keyed by the window's first part
    (0, 3, 6, 9). `fail` lists windows that raise; `prompts` records them."""
    from langchain_core.runnables import RunnableLambda

    from app.reviews import processor

    state = {"answers": {}, "fail": set(), "prompts": [], "summary": "claim"}

    def run(prompt_value):
        human = prompt_value.to_messages()[-1].content
        state["prompts"].append(human)
        excerpt = human.split('Excerpt: "', 1)[1]
        part = int(excerpt.split()[1])
        if part in state["fail"]:
            raise RuntimeError("429 quota")
        about, scope, topic = state["answers"].get(part, ("F1", "family", "build"))
        return processor._WindowAnalysis(
            summary=f"{state['summary']} {part}", about=about, sentiment="strength",
            topic=topic, scope=scope, other_laptop="ASUS V16" if about == "OTHER" else None,
        )

    class _Model:
        def with_structured_output(self, _schema):
            return RunnableLambda(run)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _Model())
    monkeypatch.setattr(processor, "embed_text", lambda t: _VEC)
    monkeypatch.setattr(processor.time, "sleep", lambda s: None)
    return state


def _rows(session, model_cls, review):
    session.expire_all()
    return sorted(
        session.exec(select(model_cls).where(model_cls.video_id == review.video_id)).all(),
        key=lambda r: r.timestamp_start_seconds,
    )


def test_each_window_of_a_versus_video_goes_where_its_answer_says(session, world, model):
    from app.reviews.models import LaptopReviewChunk, ReviewUnattachedClaim
    from app.reviews.processor import process_raw_review, processing_candidates

    review = _review(session)
    _link(session, review, world.strix)
    _link(session, review, world.zeph)
    model["answers"] = {
        0: ("F1.C2", "config", "performance"),  # the RTX 5080 config, named
        3: ("F2", "family", "build"),           # Zephyrus chassis
        6: ("OTHER", "family", "battery"),      # a laptop outside the links
        9: ("F1", "config", "performance"),     # depends on the GPU, which is unknown
    }

    out = process_raw_review(review.id, session)

    assert out["complete"] and out["destination"] == "live"
    prompt = model["prompts"][0]
    assert "F1  Strix 16 -- configurations differ in: GPU" in prompt
    assert "F1.C2  RTX 5080 Laptop GPU" in prompt
    assert "F2  Zephyrus G16 -- its configurations are identical" in prompt
    chunks = _rows(session, LaptopReviewChunk, review)
    assert [(c.timestamp_start_seconds, c.family_id, c.laptop_id, c.scope, c.topic) for c in chunks] == [
        (0, world.strix.id, world.s2.id, "config", "performance"),
        (45, world.zeph.id, None, "family", "build"),
    ]
    loose = _rows(session, ReviewUnattachedClaim, review)
    assert [(u.timestamp_start_seconds, u.reason, u.family_id, u.mentioned_laptop) for u in loose] == [
        (90, "other_laptop", None, "ASUS V16"),
        (135, "config_unknown", world.strix.id, None),
    ]
    assert review.id not in {c["review_id"] for c in processing_candidates(session)}


def test_an_id_that_was_not_offered_fails_the_window_and_is_retried(session, world, model):
    from app.reviews.processor import process_raw_review, processing_candidates

    review = _review(session)
    _link(session, review, world.strix)
    model["answers"] = {3: ("F7", "family", "build")}

    first = process_raw_review(review.id, session)
    assert (first["chunks_saved"], first["complete"]) == (3, False)
    assert first["failures"][0]["error_type"] == "InvalidAnswer"
    assert review.id in {c["review_id"] for c in processing_candidates(session)}

    model["answers"] = {}
    again = process_raw_review(review.id, session)
    assert (again["chunks_total"], again["complete"]) == (1, True)


def test_a_link_change_keeps_old_claims_public_until_the_new_set_is_complete(session, world, model):
    from app.reviews import router
    from app.reviews.link_model import ReviewLinkCreate
    from app.reviews.models import LaptopReviewChunk, RawYoutubeReview, ReviewClaimStaging
    from app.reviews.processor import process_raw_review, processing_candidates

    review = _review(session)
    _link(session, review, world.strix)
    process_raw_review(review.id, session)
    old = {c.id for c in _rows(session, LaptopReviewChunk, review)}
    assert len(old) == 4

    # A stale staged row (as if from an earlier interrupted re-run) is dropped
    # by the link change, and the review is marked.
    session.add(ReviewClaimStaging(
        video_id=review.video_id, channel_name="Chan", timestamp_start_seconds=0,
        timestamp_end_seconds=45, chunk_text="stale", embedding=_VEC, sentiment_tag="neutral",
        topic="other", reason="none"))
    session.commit()
    router.create_review_link(review.id, ReviewLinkCreate(family_id=world.zeph.id), session=session, _=None)
    session.expire_all()
    assert session.get(RawYoutubeReview, review.id).reprocess_requested_at is not None
    assert _rows(session, ReviewClaimStaging, review) == []
    assert review.id in {c["review_id"] for c in processing_candidates(session)}

    # Re-processing with one failed window: staged, not swapped; old claims stay.
    model["summary"], model["fail"] = "new", {6}
    partial = process_raw_review(review.id, session)
    assert (partial["destination"], partial["complete"], partial["swapped"]) == ("staging", False, False)
    assert {c.id for c in _rows(session, LaptopReviewChunk, review)} == old
    assert len(_rows(session, ReviewClaimStaging, review)) == 3

    # The retry completes the set and swaps it in, in one go.
    model["fail"] = set()
    done = process_raw_review(review.id, session)
    assert (done["complete"], done["swapped"]) == (True, True)
    chunks = _rows(session, LaptopReviewChunk, review)
    assert [c.chunk_text for c in chunks] == ["new 0", "new 3", "new 6", "new 9"]
    assert _rows(session, ReviewClaimStaging, review) == []
    assert session.get(RawYoutubeReview, review.id).reprocess_requested_at is None
    assert review.id not in {c["review_id"] for c in processing_candidates(session)}


def test_the_full_rerun_stages_everything_and_the_swap_rolls_back(session, world, model):
    from app.reviews.models import LaptopReviewChunk, ReviewClaimStaging, ReviewUnattachedClaim
    from app.reviews.processor import process_raw_review, swap_claims

    review = _review(session)
    _link(session, review, world.strix)
    process_raw_review(review.id, session)  # the current generation: 4 chunks

    model["summary"] = "next"
    model["answers"] = {6: ("NONE", "family", "other")}
    staged = process_raw_review(review.id, session, stage_only=True)
    assert (staged["destination"], staged["complete"], staged["swapped"]) == ("staging", True, False)
    assert [c.chunk_text for c in _rows(session, LaptopReviewChunk, review)] == [
        "claim 0", "claim 3", "claim 6", "claim 9"]  # live untouched

    swap_claims(session, [review.video_id], keep_previous=True)
    session.commit()
    assert [c.chunk_text for c in _rows(session, LaptopReviewChunk, review)] == ["next 0", "next 3", "next 9"]
    assert [u.reason for u in _rows(session, ReviewUnattachedClaim, review)] == ["none"]
    assert [s.chunk_text for s in _rows(session, ReviewClaimStaging, review)] == [
        "claim 0", "claim 3", "claim 6", "claim 9"]

    swap_claims(session, [review.video_id], keep_previous=True)  # rollback
    session.commit()
    assert [c.chunk_text for c in _rows(session, LaptopReviewChunk, review)] == [
        "claim 0", "claim 3", "claim 6", "claim 9"]
    assert _rows(session, ReviewUnattachedClaim, review) == []


def test_staged_and_unattached_claims_block_deletes(session, world, model):
    from fastapi import HTTPException

    from app.laptops import family_router, laptop_router
    from app.reviews.processor import process_raw_review

    review = _review(session)
    link = _link(session, review, world.strix, world.s1)
    model["answers"] = {6: ("F1", "config", "performance")}  # -> the link's config
    process_raw_review(review.id, session, stage_only=True)
    session.delete(link)  # so the link guard is not what refuses
    session.commit()

    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(world.s1.id, session=session)
    assert exc.value.status_code == 409 and "staged review claim" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        family_router.delete_family(world.strix.id, session)
    assert exc.value.status_code == 409 and "staged review claim" in exc.value.detail

    other = _review(session)
    zlink = _link(session, other, world.zeph)
    model["answers"] = {p: ("F1", "config", "performance") for p in (0, 3, 6, 9)}  # config unknown
    process_raw_review(other.id, session)
    session.delete(zlink)
    session.commit()
    with pytest.raises(HTTPException) as exc:
        family_router.delete_family(world.zeph.id, session)
    assert exc.value.status_code == 409 and "unattached review claim" in exc.value.detail


def test_one_claim_per_window_is_enforced(session, world):
    from sqlalchemy.exc import IntegrityError

    from app.reviews.models import LaptopReviewChunk

    for _ in range(2):
        session.add(LaptopReviewChunk(
            family_id=world.strix.id, scope="family", video_id="vdup", channel_name="c",
            timestamp_start_seconds=0, timestamp_end_seconds=45, chunk_text="x",
            embedding=_VEC, sentiment_tag="neutral"))
    with pytest.raises(IntegrityError, match="uq_review_chunk_window"):
        session.flush()
    session.rollback()


def test_price_claims_are_hidden_from_the_public_page_and_the_agent(session, world, monkeypatch):
    from app.agent.tools import laptop_tools
    from app.laptops import laptop_router
    from app.reviews.models import LaptopReviewChunk

    for start, topic, text in ((0, "price", "Only $999 in the US."), (45, "build", "Solid hinge."),
                               (90, None, "Older chunk without a topic.")):
        session.add(LaptopReviewChunk(
            family_id=world.strix.id, laptop_id=world.s1.id, scope="config", video_id="vprice",
            channel_name="Chan", timestamp_start_seconds=start, timestamp_end_seconds=start + 45,
            chunk_text=text, embedding=[1.0] + [0.0] * 767, sentiment_tag="strength", topic=topic))
    session.commit()

    page = laptop_router.get_laptop_reviews(world.s1.id, session=session)
    assert {p["text"] for p in page["strengths"]} == {"Solid hinge.", "Older chunk without a topic."}

    monkeypatch.setattr(laptop_tools, "engine", session.connection())
    monkeypatch.setattr(laptop_tools, "embed_text", lambda q: [1.0] + [0.0] * 767)
    monkeypatch.setattr(laptop_tools, "settings", SimpleNamespace(review_evidence_max_distance=0.44))
    out = laptop_tools.get_review_evidence.invoke({"laptop_id": str(world.s1.id), "query_context": "price"})
    assert "Solid hinge." in out and "$999" not in out
