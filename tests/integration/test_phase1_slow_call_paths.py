"""
Phase 1 slow-call paths against real Postgres + pgvector.

The unit tier proves no pooled connection is checked out during the slow call.
It cannot prove the rows those paths read are still usable afterwards: SQLite
with canned reads has no real ORM identity map, no pgvector, and no expiry on
commit. Here every path runs on real rows, real Vector(768) columns and real
SQL, with only the external call (Gemini, embedding, YouTube, HTTP) faked --
so a DetachedInstanceError, an expired attribute read on a closed session, or
a pgvector query broken by the reordering shows up as a failure.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import select

from tests.integration.conftest import make_laptop

_VEC = [0.01] * 768


class _FakeChatModel:
    output = None

    def __init__(self, **_kw):
        pass

    def with_structured_output(self, _schema):
        from langchain_core.runnables import RunnableLambda
        return RunnableLambda(lambda _payload: type(self).output())


@pytest.fixture
def catalog(session, brand):
    from app.laptops.laptop_models import LaptopEmbedding

    laptops = [make_laptop(brand.id, product_name=f"TUF F16 {i}", price_rm=4000.0 + i * 500) for i in range(3)]
    for laptop in laptops:
        session.add(laptop)
    session.commit()
    for i, laptop in enumerate(laptops):
        session.add(LaptopEmbedding(laptop_id=laptop.id, embedding=[0.01 + i * 0.001] * 768))
    session.commit()
    return laptops


@pytest.fixture
def cold_scoring_cache(monkeypatch):
    """Force get_scoring_data's inline build, the path that now commits."""
    from app.pickscore import ranges_cache

    monkeypatch.setattr(ranges_cache, "_data", None)
    monkeypatch.setattr(ranges_cache, "_built_generation", -1)
    monkeypatch.setattr(ranges_cache, "_start_rebuild", lambda build: None)


def test_search_laptops_semantic_and_fallback_on_real_rows(session, catalog, cold_scoring_cache, monkeypatch):
    import sys

    import app.database as database
    from app.agent.tools import search_laptops as _pkg  # noqa: F401
    from app.rag import retrieval

    tool = sys.modules["app.agent.tools.search_laptops"]
    connection = session.connection()
    # Captured once: the loop swaps app.database.engine itself, and comparing
    # against it afterwards would match nothing (same as conftest._app_client).
    proxy = database.engine
    for module in [m for m in list(sys.modules.values()) if getattr(m, "__name__", "").startswith("app")]:
        for name, value in list(vars(module).items()):
            if value is proxy:
                monkeypatch.setattr(module, name, connection)
    monkeypatch.setattr(retrieval, "_QUERY_CACHE", {})
    monkeypatch.setattr(retrieval, "_FAILED_QUERIES", {})

    monkeypatch.setattr(retrieval, "embed_text", lambda q: _VEC)
    semantic = tool._run_search("gaming laptop", budget_max=6000.0)
    assert semantic["results"], semantic
    # No benchmark rows seeded -> withheld (ADR-0016). Scoring ran on the
    # cold path, whose commit expired the candidate rows; building each result
    # dict afterwards reloaded them on the still-open tool session.
    assert all(r["pick_score"] is None and r["pick_score_unavailable"] for r in semantic["results"])
    assert all(r["product_name"] and r["model_code"] for r in semantic["results"])

    def down(_q):
        raise RuntimeError("embedding API down")

    monkeypatch.setattr(retrieval, "embed_text", down)
    fallback = tool._run_search("a different query", budget_max=6000.0)
    assert fallback["results"], fallback
    assert {r["laptop_id"] for r in fallback["results"]} <= {str(l.id) for l in catalog}


def _channel(session):
    from app.reviews.models import YoutubeChannel

    ch = YoutubeChannel(channel_id=f"UC{uuid.uuid4().hex[:10]}", channel_name="Chan", active=True)
    session.add(ch)
    session.commit()
    return ch


class _Guard:
    tripped = False
    attempted = 0

    def __init__(self, ok=True):
        from app.reviews.transcript import TranscriptFailure, TranscriptResult

        self._result = (
            TranscriptResult(segments=[{"start": 0.0, "duration": 5.0, "text": "fast"}], language_code="en")
            if ok else TranscriptResult(segments=None, failure=TranscriptFailure.IP_BLOCKED)
        )

    def fetch(self, _video_id):
        self.attempted += 1
        return self._result


def test_ingest_new_rows_then_existing_rejected_rows(session, catalog, monkeypatch):
    from app.reviews import service
    from app.reviews.models import RawYoutubeReview

    ch = _channel(session)
    channel_id = ch.channel_id
    vids = [f"v{uuid.uuid4().hex[:8]}" for _ in range(2)]
    monkeypatch.setattr(service, "discover_videos", lambda b, p, channels: [
        {"video_id": v, "channel_id": channels[0].channel_id, "video_title": "Asus TUF F16 0 review"}
        for v in vids
    ])

    first = service.ingest_for_laptop(catalog[0].id, session, guard=_Guard(ok=False))
    assert first["rejected"] == 2
    # Second run hits the `existing` branch: the row was loaded, then expired
    # by the commit before the fetch, then mutated -- a real reload.
    second = service.ingest_for_laptop(catalog[0].id, session, guard=_Guard(ok=True))
    assert second["rejected"] == 0 and second["matched"] + second["pending"] == 2

    rows = session.exec(select(RawYoutubeReview).where(RawYoutubeReview.video_id.in_(vids))).all()
    assert {r.channel_id for r in rows} == {channel_id}
    assert all(r.transcript_attempts == 2 and r.raw_transcript["segments"] for r in rows)


def test_ingest_bulk_on_real_rows(session, catalog, monkeypatch):
    from app.reviews import service

    _channel(session)
    monkeypatch.setattr(service, "discover_videos", lambda b, p, channels: [])
    monkeypatch.setattr(service, "TranscriptFetchGuard", lambda: _Guard())
    out = service.ingest_bulk(session, limit=5, skip_covered=False)
    assert out["families_attempted"] >= 1
    assert all("error" not in r for r in out["results"]), out["results"]


def _raw_review(session, channel_id, **kw):
    from app.reviews.models import RawYoutubeReview

    row = RawYoutubeReview(
        video_id=f"v{uuid.uuid4().hex[:8]}", channel_id=channel_id, video_title="TUF F16 review",
        raw_transcript={}, **kw,
    )
    session.add(row)
    session.commit()
    return row


def test_retry_transcripts_on_real_rows(session, monkeypatch):
    from app.reviews import router
    from app.reviews.models import RawYoutubeReview

    ch = _channel(session)
    ids = [_raw_review(session, ch.channel_id, status="rejected", failure_reason="ip_blocked").id for _ in range(2)]
    monkeypatch.setattr(router, "TranscriptFetchGuard", lambda **k: _Guard(ok=True))

    out = router.retry_transcripts(limit=20, delay_seconds=0, max_consecutive_blocks=3, session=session, _=None)

    assert out["recovered"] >= 2
    rows = session.exec(select(RawYoutubeReview).where(RawYoutubeReview.id.in_(ids))).all()
    assert all(r.status == "pending" and r.transcript_attempts == 1 and r.raw_transcript["segments"] for r in rows)


def test_backfill_descriptions_on_real_rows(session, monkeypatch):
    from app.reviews import router
    from app.reviews.models import RawYoutubeReview

    ch = _channel(session)
    row_id = _raw_review(session, ch.channel_id, status="pending").id
    monkeypatch.setattr(router, "fetch_descriptions", lambda ids: {i: "spec table" for i in ids})

    out = router.backfill_descriptions(limit=1000, session=session, _=None)

    assert out["filled"] >= 1 and out["still_missing"] == 0
    assert session.get(RawYoutubeReview, row_id).video_description == "spec table"


def test_process_raw_review_on_real_rows(session, catalog, monkeypatch):
    from app.reviews import processor
    from app.reviews.models import LaptopReviewChunk

    from app.laptops.family_model import LaptopFamily
    from app.reviews.link_model import ReviewLaptopLink

    ch = _channel(session)
    raw = _raw_review(session, ch.channel_id, status="matched")
    family = LaptopFamily(brand_id=catalog[0].brand_id, name="TUF F16")
    session.add(family)
    session.commit()
    catalog[0].family_id = family.id
    session.add(catalog[0])
    session.add(ReviewLaptopLink(raw_review_id=raw.id, family_id=family.id, laptop_id=catalog[0].id))
    session.commit()
    raw.raw_transcript = {"segments": [{"start": float(s), "duration": 5.0, "text": "fast"} for s in range(0, 100, 5)]}
    session.add(raw)
    session.commit()
    monkeypatch.setattr(processor.time, "sleep", lambda s: None)
    monkeypatch.setattr(processor, "embed_text", lambda t: _VEC)
    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _FakeChatModel())
    _FakeChatModel.output = lambda: processor._WindowAnalysis(
        summary="fast", about="F1", sentiment="strength", topic="performance", scope="config",
    )

    report = processor.process_raw_review(raw.id, session)

    assert report["chunks_failed"] == 0, report["failures"]
    chunks = session.exec(select(LaptopReviewChunk).where(LaptopReviewChunk.laptop_id == catalog[0].id)).all()
    assert len(chunks) == report["chunks_saved"] >= 2
    assert len(chunks[0].embedding) == 768


def test_google_avatar_import_on_real_user(session, api_user, monkeypatch):
    from app.users import router
    from app.users.avatar_model import UserAvatar

    session.refresh(api_user)  # what the login route does right before
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    monkeypatch.setattr(router.requests, "get", lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, content=png))

    router._import_google_avatar(session, api_user, "https://x/a=s96-c")

    assert str(api_user.id)  # caller then issues the token from this object
    assert session.exec(select(UserAvatar).where(UserAvatar.user_id == api_user.id)).first() is not None


def test_generate_single_embedding_on_real_row(session, catalog, monkeypatch):
    from app.embeddings import service
    from app.laptops.laptop_models import LaptopEmbedding

    monkeypatch.setattr(service, "embed_text", lambda t: [0.5] * 768)

    result = service.generate_single_laptop_embedding(session, catalog[1].id)

    assert result["status"] == "success", result
    emb = session.exec(select(LaptopEmbedding).where(LaptopEmbedding.laptop_id == catalog[1].id)).one()
    assert float(emb.embedding[0]) == pytest.approx(0.5)
