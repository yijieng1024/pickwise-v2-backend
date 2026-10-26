"""
Review A: the review pipeline reads and writes review_laptop_link only (A2
dropped the old single-laptop column), and its three known holes stay shut --
reprocessing a finished review, a partial review counting as done, and
matching overwriting a human decision.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlmodel import select

from tests._progress import RecordingProgress
from tests.integration.conftest import make_laptop

_VEC = [0.01] * 768
# Refused requests never reach create_job, so the admin's id is never read.
_ADMIN = SimpleNamespace(id=None)


@pytest.fixture
def family_laptops(session, brand):
    from app.laptops.family_model import LaptopFamily

    family = LaptopFamily(brand_id=brand.id, name="TUF F16", family_key="tuf f16")
    session.add(family)
    session.commit()
    laptops = [
        make_laptop(brand.id, product_name=f"TUF F16 {i}", family_id=family.id)
        for i in range(2)
    ]
    session.add_all(laptops)
    session.commit()
    return family, laptops


def _review(session, status="matched", segments=12, **kw):
    from app.reviews.models import RawYoutubeReview, YoutubeChannel

    ch = YoutubeChannel(channel_id=f"UC{uuid.uuid4().hex[:10]}", channel_name="Chan", active=True)
    session.add(ch)
    session.commit()
    row = RawYoutubeReview(
        video_id=f"v{uuid.uuid4().hex[:8]}", channel_id=ch.channel_id,
        video_title="TUF F16 review", status=status,
        # 12 x 15 s segments -> four 45-second windows.
        raw_transcript={"segments": [
            {"start": float(i * 15), "duration": 15.0, "text": f"part {i}"} for i in range(segments)
        ]},
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _link(session, review, family, laptop=None, source="auto", confidence=80.0):
    from app.reviews.link_model import ReviewLaptopLink

    link = ReviewLaptopLink(
        raw_review_id=review.id, family_id=family.id,
        laptop_id=laptop.id if laptop else None, match_source=source,
        match_confidence=confidence if source == "auto" else None,
    )
    session.add(link)
    session.commit()
    return link


@pytest.fixture
def fake_llm(monkeypatch):
    """Gemini + embeddings faked. `fail_at` makes those window starts raise;
    `about` / `scope` are the model's answer for every window."""
    from langchain_core.runnables import RunnableLambda

    from app.reviews import processor

    state = {"fail_at": set(), "about": "F1", "scope": "family"}

    class _Model:
        def __init__(self, **_kw):
            pass

        def with_structured_output(self, _schema):
            def run(prompt_value):
                # The excerpt only: the prompt also carries the previous window's tail.
                text = prompt_value.to_messages()[-1].content.split('Excerpt: "', 1)[1]
                if any(f"part {i}" in text for i in state["fail_at"]):
                    raise RuntimeError("429 quota")
                return processor._WindowAnalysis(
                    summary="ok", about=state["about"], sentiment="neutral",
                    topic="other", scope=state["scope"],
                )
            return RunnableLambda(run)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _Model())
    monkeypatch.setattr(processor, "embed_text", lambda t: _VEC)
    monkeypatch.setattr(processor.time, "sleep", lambda s: None)
    from types import SimpleNamespace

    from app.reviews import router
    monkeypatch.setattr(router, "settings", SimpleNamespace(review_processing_paused=False))
    return state


def _chunks(session, review):
    from app.reviews.models import LaptopReviewChunk

    return session.exec(
        select(LaptopReviewChunk).where(LaptopReviewChunk.video_id == review.video_id)
    ).all()


# --- 1. duplicate guard ------------------------------------------------------

def test_a_finished_review_is_never_processed_again(session, family_laptops, fake_llm):
    from fastapi import HTTPException

    from app.reviews import jobs, router
    from app.reviews.processor import processing_candidates

    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])

    first = jobs.process_one_worker(review.id, review.video_title)(session, RecordingProgress())["results"][0]
    assert first["complete"] and first["chunks_saved"] == 4
    assert review.id not in {c["review_id"] for c in processing_candidates(session)}

    # Refused before a job exists: nothing left to process.
    with pytest.raises(HTTPException) as exc:
        router.process_review(review.id, background_tasks=BackgroundTasks(), session=session, admin=_ADMIN)
    assert exc.value.status_code == 409
    assert jobs.process_bulk_worker(50)(session, RecordingProgress())["candidates"] == 0
    assert len(_chunks(session, review)) == 4


def test_processing_routes_are_paused_by_default(session, family_laptops):
    """Until the Review C accuracy check is done (REVIEW_PROCESSING_PAUSED)."""
    from fastapi import HTTPException

    from app.config import Settings
    from app.reviews import router

    assert Settings.model_fields["review_processing_paused"].default is True
    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])
    for call in (lambda: router.process_review(review.id, background_tasks=BackgroundTasks(), session=session, admin=_ADMIN),
                 lambda: router.process_bulk(background_tasks=BackgroundTasks(), limit=5, session=session, admin=_ADMIN)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 409 and "REVIEW_PROCESSING_PAUSED" in exc.value.detail
    assert _chunks(session, review) == []


# --- 2. partial processing ---------------------------------------------------

def test_a_partial_review_is_reported_and_retried_for_its_missing_windows_only(
    session, family_laptops, fake_llm
):
    from app.reviews import jobs
    from app.reviews.processor import process_raw_review, processing_candidates

    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])

    fake_llm["fail_at"] = {3}  # window 2 (segments 3-5) fails
    first = process_raw_review(review.id, session)
    assert (first["chunks_saved"], first["chunks_failed"], first["complete"]) == (3, 1, False)
    assert first["failures"][0]["error_type"] == "RuntimeError"
    assert first["failures"][0]["start_seconds"] == 45

    partial = [c for c in processing_candidates(session) if c["review_id"] == review.id]
    assert partial and (partial[0]["windows_saved"], partial[0]["windows_expected"]) == (3, 4)

    fake_llm["fail_at"] = set()
    bulk = jobs.process_bulk_worker(50)(session, RecordingProgress())
    result = next(r for r in bulk["results"] if r["review_id"] == str(review.id))
    assert (result["windows_already_saved"], result["chunks_total"], result["complete"]) == (3, 1, True)
    starts = sorted(c.timestamp_start_seconds for c in _chunks(session, review))
    assert starts == [0, 45, 90, 135]  # one per window, no duplicate


@pytest.fixture
def other_family(session, brand):
    from app.laptops.family_model import LaptopFamily

    family = LaptopFamily(brand_id=brand.id, name="Zephyrus G16", family_key="zephyrus g16")
    session.add(family)
    session.commit()
    return family


def test_processing_follows_links_not_the_column(session, family_laptops, other_family, fake_llm):
    from app.reviews.processor import process_raw_review

    family, laptops = family_laptops
    fake_llm["scope"] = "config"
    # MATCHED with no link (the parked production rows): refused.
    parked = _review(session)
    with pytest.raises(ValueError, match="not processable"):
        process_raw_review(parked.id, session)

    # A human config link wins over an auto link naming a different config.
    human = _review(session)
    _link(session, human, family, laptops[0], source="auto")
    _link(session, human, family, laptops[1], source="human")
    process_raw_review(human.id, session)
    assert {(c.laptop_id, c.family_id, c.scope) for c in _chunks(session, human)} == {
        (laptops[1].id, family.id, "config")}

    # Several families (a versus video) are processable since Review C: each
    # window is assigned on its own (tests/integration/test_review_c.py).
    versus = _review(session)
    _link(session, versus, family, laptops[0], source="human")
    _link(session, versus, other_family, None, source="human")
    assert process_raw_review(versus.id, session)["complete"]


def test_a_family_only_review_writes_family_scope_chunks(session, family_laptops, fake_llm):
    from app.reviews.processor import process_raw_review, processing_candidates

    family, laptops = family_laptops
    # A human family-only link overrules an auto config link (Review B: it used
    # to be refused, now it is processed at family scope).
    review = _review(session)
    _link(session, review, family, laptops[0], source="auto")
    _link(session, review, family, None, source="human")
    assert review.id in {c["review_id"] for c in processing_candidates(session)}

    out = process_raw_review(review.id, session)

    assert out["complete"] and out["chunks_saved"] == 4
    assert {(c.laptop_id, c.family_id, c.scope) for c in _chunks(session, review)} == {
        (None, family.id, "family")}
    assert review.id not in {c["review_id"] for c in processing_candidates(session)}
    from app.reviews.processor import AlreadyProcessed
    with pytest.raises(AlreadyProcessed):
        process_raw_review(review.id, session)


def test_a_config_scope_chunk_must_name_its_laptop(session, family_laptops):
    """The CHECK constraint, not just the processor: a config-scope claim is
    about one configuration."""
    from sqlalchemy.exc import IntegrityError

    from app.reviews.models import LaptopReviewChunk

    family, _ = family_laptops
    session.add(LaptopReviewChunk(
        laptop_id=None, family_id=family.id, scope="config", video_id="v1",
        channel_name="c", timestamp_start_seconds=0, timestamp_end_seconds=45,
        chunk_text="x", embedding=_VEC, sentiment_tag="neutral"))
    with pytest.raises(IntegrityError, match="ck_review_chunk_config_has_laptop"):
        session.flush()
    session.rollback()


# --- 3. /rematch never touches a human-linked review ------------------------

def test_rematch_skips_human_linked_reviews(session, family_laptops, monkeypatch):
    from app.reviews import jobs
    from app.reviews.link_model import ReviewLaptopLink
    from app.reviews.models import RawYoutubeReview

    family, laptops = family_laptops
    human = _review(session, status="pending")
    _link(session, human, family, None, source="human")
    plain = _review(session, status="pending")
    monkeypatch.setattr(jobs, "match_laptop", lambda title, s: (laptops[0].id, 90.0))

    out = jobs.rematch_worker()(session, RecordingProgress())

    assert out["skipped_human_linked"] >= 1
    session.expire_all()
    assert session.get(RawYoutubeReview, human.id).status == "pending"
    human_links = session.exec(select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == human.id)).all()
    assert [(l.match_source, l.laptop_id) for l in human_links] == [("human", None)]

    plain_row = session.get(RawYoutubeReview, plain.id)
    assert plain_row.status == "matched"
    auto = session.exec(select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == plain.id)).all()
    assert [(l.match_source, l.laptop_id, l.match_confidence) for l in auto] == [("auto", laptops[0].id, 90.0)]

    # A second rematch replaces the auto guess rather than stacking a duplicate.
    plain_row.status = "pending"
    session.add(plain_row)
    session.commit()
    jobs.rematch_worker()(session, RecordingProgress())
    assert len(session.exec(select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == plain.id)).all()) == 1


# --- 4. ingest never rewrites a human-linked rejected review ----------------

class _Guard:
    tripped = False

    def fetch(self, _video_id):
        from app.reviews.transcript import TranscriptResult

        return TranscriptResult(segments=[{"start": 0.0, "duration": 5.0, "text": "x"}], language_code="en")


def test_ingest_skips_human_linked_rejected_and_links_new_matches(session, family_laptops, monkeypatch):
    from app.reviews import service
    from app.reviews.link_model import ReviewLaptopLink
    from app.reviews.models import RawYoutubeReview

    family, laptops = family_laptops
    rejected = _review(session, status="rejected", segments=0, failure_reason="ip_blocked")
    _link(session, rejected, family, None, source="human")
    new_vid = f"v{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(service, "discover_videos", lambda b, p, channels: [
        {"video_id": v, "channel_id": rejected.channel_id, "video_title": "TUF F16 review"}
        for v in (rejected.video_id, new_vid)
    ])
    monkeypatch.setattr(service, "match_laptop", lambda title, s: (laptops[1].id, 88.0))

    counts = service.ingest_for_laptop(laptops[0].id, session, guard=_Guard())

    assert counts["skipped"] == 1 and counts["matched"] == 1
    session.expire_all()
    kept = session.get(RawYoutubeReview, rejected.id)
    assert (kept.status, kept.raw_transcript, kept.transcript_attempts) == ("rejected", {"segments": []}, 0)
    assert [l.match_source for l in session.exec(
        select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == rejected.id))] == ["human"]

    new = session.exec(select(RawYoutubeReview).where(RawYoutubeReview.video_id == new_vid)).one()
    assert new.status == "matched"
    link = session.exec(select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == new.id)).one()
    assert (link.match_source, link.laptop_id, link.family_id, link.match_confidence) == (
        "auto", laptops[1].id, family.id, 88.0)


# --- 5. manual match + queue + link deletion ---------------------------------

def test_manual_match_writes_a_human_link_and_leaves_the_column_alone(session, family_laptops):
    from app.reviews import router
    from app.reviews.models import ManualMatchRequest, RawYoutubeReview

    family, laptops = family_laptops
    review = _review(session, status="pending", match_confidence=61.0)

    out = router.manual_match(review.id, ManualMatchRequest(laptop_id=laptops[0].id), session=session, _=None)

    assert [(l["match_source"], l["match_confidence"]) for l in out["links"]] == [("human", None)]
    assert out["review"].matched_laptop_id == laptops[0].id  # API field, resolved from the link
    row = session.get(RawYoutubeReview, review.id)
    assert (row.status, row.match_confidence) == ("matched", 61.0)


def test_the_queue_shows_matched_reviews_that_cannot_be_processed(
    session, family_laptops, other_family
):
    from app.common.pagination_service import PaginationParams
    from app.reviews import router

    family, laptops = family_laptops
    parked = _review(session)                                    # matched, no link
    versus = _review(session)                                    # several families: processable (Review C)
    _link(session, versus, family, laptops[0], source="human")
    _link(session, versus, other_family, None, source="human")
    family_only = _review(session)                               # processable now
    _link(session, family_only, family, None, source="human")
    ready = _review(session)
    _link(session, ready, family, laptops[0], source="auto")

    def queue():
        page = router.list_pending_reviews(
            pagination=PaginationParams(skip=0, limit=100), search=None, session=session, _=None)
        return {item["id"] for item in page.items}

    ids = queue()
    assert parked.id in ids
    assert ready.id not in ids and family_only.id not in ids and versus.id not in ids
    status = router.pipeline_status(session=session, _=None)
    assert status["link"]["pending_total"] == len(ids)

    # Deleting the only config link sends a matched review back to the queue.
    link_id = router.links_for_reviews(session, [ready.id])[ready.id][0]["id"]
    router.delete_review_link(link_id, session=session, _=None)
    assert ready.id in queue()


@pytest.mark.parametrize("paused", [True, False])
def test_pipeline_status_reports_the_processing_pause(session, monkeypatch, paused):
    """The admin disables Run processing / Summarize transcript from this
    field, so it must say exactly what _refuse_while_paused will do."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.reviews import router

    monkeypatch.setattr(router, "settings", SimpleNamespace(review_processing_paused=paused))
    assert router.pipeline_status(session=session, _=None)["process"]["paused"] is paused
    if paused:
        with pytest.raises(HTTPException) as exc:
            router._refuse_while_paused()
        assert exc.value.status_code == 409
    else:
        router._refuse_while_paused()


def test_a_linked_laptop_cannot_be_deleted(session, family_laptops):
    from fastapi import HTTPException

    from app.laptops import laptop_router

    family, laptops = family_laptops
    _link(session, _review(session), family, laptops[0])
    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(laptops[0].id, session=session)
    assert exc.value.status_code == 409 and "review link" in exc.value.detail


# --- 6. a human link moves a PENDING review to MATCHED (Review B) -----------

def test_a_human_link_promotes_pending_only(session, family_laptops):
    from app.reviews import router
    from app.reviews.link_model import ReviewLinkCreate
    from app.reviews.models import ManualMatchRequest, RawYoutubeReview

    family, laptops = family_laptops
    pending = _review(session, status="pending")
    rejected = _review(session, status="rejected", segments=0)
    irrelevant = _review(session, status="irrelevant")
    for r in (pending, rejected, irrelevant):
        router.create_review_link(r.id, ReviewLinkCreate(family_id=family.id), session=session, _=None)

    via_patch = _review(session, status="pending")
    router.manual_match(via_patch.id, ManualMatchRequest(family_id=family.id), session=session, _=None)

    session.expire_all()
    status = lambda r: session.get(RawYoutubeReview, r.id).status
    assert (status(pending), status(rejected), status(irrelevant), status(via_patch)) == (
        "matched", "rejected", "irrelevant", "matched")


def test_a_family_with_review_chunks_cannot_be_deleted(session, family_laptops, fake_llm):
    from fastapi import HTTPException

    from app.laptops import family_router
    from app.reviews.processor import process_raw_review

    family, laptops = family_laptops
    review = _review(session)
    link = _link(session, review, family, None, source="human")
    process_raw_review(review.id, session)
    session.delete(link)  # so the link guard is not what refuses
    session.commit()

    with pytest.raises(HTTPException) as exc:
        family_router.delete_family(family.id, session)
    assert exc.value.status_code == 409 and "review chunk" in exc.value.detail
