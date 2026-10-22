"""
Review A: the review pipeline reads and writes review_laptop_link, never
raw_youtube_reviews.matched_laptop_id, and its three known holes stay shut --
reprocessing a finished review, a partial review counting as done, and
matching overwriting a human decision.

Every test also asserts the column stays NULL: "nothing writes it" is the
property A2's DROP COLUMN relies on.
"""
import uuid

import pytest
from sqlmodel import select

from tests.integration.conftest import make_laptop

_VEC = [0.01] * 768


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
    """Gemini + embeddings faked. `fail_at` makes those window starts raise."""
    from langchain_core.runnables import RunnableLambda

    from app.reviews import processor

    state = {"fail_at": set()}

    class _Model:
        def __init__(self, **_kw):
            pass

        def with_structured_output(self, _schema):
            def run(prompt_value):
                text = prompt_value.to_messages()[-1].content
                if any(f"part {i}" in text for i in state["fail_at"]):
                    raise RuntimeError("429 quota")
                return processor._ChunkAnalysis(summary="ok", sentiment_tag="neutral")
            return RunnableLambda(run)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _Model())
    monkeypatch.setattr(processor, "embed_text", lambda t: _VEC)
    monkeypatch.setattr(processor.time, "sleep", lambda s: None)
    return state


def _chunks(session, review):
    from app.reviews.models import LaptopReviewChunk

    return session.exec(
        select(LaptopReviewChunk).where(LaptopReviewChunk.video_id == review.video_id)
    ).all()


# --- 1. duplicate guard ------------------------------------------------------

def test_a_finished_review_is_never_processed_again(session, family_laptops, fake_llm):
    from fastapi import HTTPException

    from app.reviews import router
    from app.reviews.processor import processing_candidates

    family, laptops = family_laptops
    review = _review(session)
    _link(session, review, family, laptops[0])

    first = router.process_review(review.id, session=session, _=None)
    assert first["complete"] and first["chunks_saved"] == 4
    assert review.id not in {c["review_id"] for c in processing_candidates(session)}

    with pytest.raises(HTTPException) as exc:
        router.process_review(review.id, session=session, _=None)
    assert exc.value.status_code == 409
    assert router.process_bulk(limit=50, session=session, _=None)["candidates"] == 0
    assert len(_chunks(session, review)) == 4
    assert review.matched_laptop_id is None


# --- 2. partial processing ---------------------------------------------------

def test_a_partial_review_is_reported_and_retried_for_its_missing_windows_only(
    session, family_laptops, fake_llm
):
    from app.reviews import router
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
    bulk = router.process_bulk(limit=50, session=session, _=None)
    result = next(r for r in bulk["results"] if r["review_id"] == str(review.id))
    assert (result["windows_already_saved"], result["chunks_total"], result["complete"]) == (3, 1, True)
    starts = sorted(c.timestamp_start_seconds for c in _chunks(session, review))
    assert starts == [0, 45, 90, 135]  # one per window, no duplicate


def test_processing_follows_links_not_the_column(session, family_laptops, fake_llm):
    from app.reviews.processor import process_raw_review

    family, laptops = family_laptops
    # Column set (as the 3 parked production rows are) but no link: refused.
    parked = _review(session, matched_laptop_id=laptops[0].id)
    with pytest.raises(ValueError, match="not processable"):
        process_raw_review(parked.id, session)

    # An auto config link overruled by a human family-only link: refused.
    overruled = _review(session)
    _link(session, overruled, family, laptops[0], source="auto")
    _link(session, overruled, family, None, source="human")
    with pytest.raises(ValueError, match="not processable"):
        process_raw_review(overruled.id, session)

    # A human config link wins over an auto link naming a different config.
    human = _review(session)
    _link(session, human, family, laptops[0], source="auto")
    _link(session, human, family, laptops[1], source="human")
    process_raw_review(human.id, session)
    assert {c.laptop_id for c in _chunks(session, human)} == {laptops[1].id}


# --- 3. /rematch never touches a human-linked review ------------------------

def test_rematch_skips_human_linked_reviews(session, family_laptops, monkeypatch):
    from app.reviews import router
    from app.reviews.link_model import ReviewLaptopLink
    from app.reviews.models import RawYoutubeReview

    family, laptops = family_laptops
    human = _review(session, status="pending")
    _link(session, human, family, None, source="human")
    plain = _review(session, status="pending")
    monkeypatch.setattr(router, "match_laptop", lambda title, s: (laptops[0].id, 90.0))

    out = router.rematch_pending(session=session, _=None)

    assert out["skipped_human_linked"] >= 1
    session.expire_all()
    assert session.get(RawYoutubeReview, human.id).status == "pending"
    human_links = session.exec(select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == human.id)).all()
    assert [(l.match_source, l.laptop_id) for l in human_links] == [("human", None)]

    plain_row = session.get(RawYoutubeReview, plain.id)
    assert plain_row.status == "matched" and plain_row.matched_laptop_id is None
    auto = session.exec(select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == plain.id)).all()
    assert [(l.match_source, l.laptop_id, l.match_confidence) for l in auto] == [("auto", laptops[0].id, 90.0)]

    # A second rematch replaces the auto guess rather than stacking a duplicate.
    plain_row.status = "pending"
    session.add(plain_row)
    session.commit()
    router.rematch_pending(session=session, _=None)
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
    assert new.status == "matched" and new.matched_laptop_id is None
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
    assert out["review"].matched_laptop_id == laptops[0].id  # resolved from the link
    row = session.get(RawYoutubeReview, review.id)
    assert (row.status, row.matched_laptop_id, row.match_confidence) == ("matched", None, 61.0)


def test_the_queue_shows_matched_reviews_that_cannot_be_processed(session, family_laptops):
    from app.common.pagination_service import PaginationParams
    from app.reviews import router

    family, laptops = family_laptops
    parked = _review(session, matched_laptop_id=laptops[0].id)   # column, no link
    overruled = _review(session)
    _link(session, overruled, family, laptops[0], source="auto")
    _link(session, overruled, family, None, source="human")
    ready = _review(session)
    _link(session, ready, family, laptops[0], source="auto")

    def queue():
        page = router.list_pending_reviews(
            pagination=PaginationParams(skip=0, limit=100), search=None, session=session, _=None)
        return {item["id"] for item in page.items}

    ids = queue()
    assert parked.id in ids and overruled.id in ids and ready.id not in ids
    status = router.pipeline_status(session=session, _=None)
    assert status["link"]["pending_total"] == len(ids)

    # Deleting the only config link sends a matched review back to the queue.
    link_id = router.links_for_reviews(session, [ready.id])[ready.id][0]["id"]
    router.delete_review_link(link_id, session=session, _=None)
    assert ready.id in queue()


def test_a_linked_laptop_cannot_be_deleted(session, family_laptops):
    from fastapi import HTTPException

    from app.laptops import laptop_router

    family, laptops = family_laptops
    _link(session, _review(session), family, laptops[0])
    with pytest.raises(HTTPException) as exc:
        laptop_router.delete_laptop(laptops[0].id, session=session)
    assert exc.value.status_code == 409 and "review link" in exc.value.detail
