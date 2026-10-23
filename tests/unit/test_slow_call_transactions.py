"""
No database connection may be checked out while a slow external call runs.

Production hit "QueuePool limit of size 20 overflow 20 reached" with no leaked
sessions: a read opened a transaction, and the session kept its connection
through the Gemini / embedding / YouTube / HTTP call that followed. Each test
here runs one real call site against a real SQLAlchemy Session on a QueuePool
(a throwaway SQLite file, so no server), replaces the slow call with a probe,
and the probe asserts `pool.checkedout() == 0` -- i.e. the connection really
went back to the pool, not merely that some method was called.

The database reads are faked at the session (canned rows), but each fake read
first calls `session.connection()`, which checks a connection out and begins a
transaction exactly as a real query does. That is the state the bug lived in.

Also: a real chat model and the real embedder, pointed at a local socket that
accepts and never answers, must raise within their timeout. Loopback only.
"""

import functools
import socket
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool
from sqlmodel import Session

from ._adapters import (
    _pickscore_adapter,
    _retrieval,
    _search_tool,
    agent_graph,
    embeddings_service,
    llm_module,
    Settings,
    review_processor,
    review_router,
    review_service,
    TranscriptResult as _TranscriptResult,
    users_router,
)

@pytest.fixture
def db(tmp_path):
    """(engine, session) on a pooled SQLite file. The tables never exist;
    every read is canned by _reads below."""
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}", poolclass=QueuePool)
    session = Session(engine)
    yield engine, session
    session.close()
    engine.dispose()


class _Result:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value[0] if isinstance(self._value, list) and self._value else (
            None if isinstance(self._value, list) else self._value
        )

    def all(self):
        return self._value

    def one(self):
        return self._value


def _reads(monkeypatch, session, *answers):
    """Replace session.exec/execute with canned answers, in call order. Each
    call checks out a connection first, the way a real query does."""
    queue = list(answers)

    def read(*_a, **_k):
        session.connection()
        return _Result(queue.pop(0))

    monkeypatch.setattr(session, "exec", read)
    monkeypatch.setattr(session, "execute", read)
    monkeypatch.setattr(session, "add", lambda obj: None)
    monkeypatch.setattr(session, "expunge", lambda obj: None)  # canned rows are not ORM instances


def _probe(engine, calls, name, result=None):
    def call(*_a, **_k):
        assert engine.pool.checkedout() == 0, f"{name} ran with a DB connection checked out"
        calls.append(name)
        return result() if callable(result) else result
    return call


def _laptop(**kw):
    base = dict(
        id=uuid.uuid4(), brand_id=uuid.uuid4(), product_name="TUF F16", price_rm=4999.0,
        processor_model="i7", gpu_model="RTX 4060", ram_gb=16, ssd_gb=512,
        storage_type="SSD", weight_kg=2.2, battery_wh=90.0, display_size_inch=16.0,
        model_code="tuf-f16", family_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeChatModel:
    """Stands in for build_llm's model; the structured runnable is the probe."""

    probe = None

    def __init__(self, **_kw):
        pass

    def with_structured_output(self, _schema):
        from langchain_core.runnables import RunnableLambda
        return RunnableLambda(lambda _payload: type(self).probe())


# --------------------------------------------------------------------------
# agent/tools/search_laptops.py
# --------------------------------------------------------------------------
def test_search_laptops_embeds_before_opening_a_transaction(db, monkeypatch):
    engine, session = db
    calls = []
    monkeypatch.setattr(_retrieval, "_QUERY_CACHE", {})
    monkeypatch.setattr(_retrieval, "_FAILED_QUERIES", {}, raising=False)
    monkeypatch.setattr(_retrieval, "embed_text", _probe(engine, calls, "embed", [0.0] * 768))
    monkeypatch.setattr(_search_tool, "Session", lambda *a, **k: session)

    def retrieve(query, sess, **_k):
        sess.connection()  # the pgvector query
        _retrieval._get_query_vector(query)  # what the real function does next
        return []

    monkeypatch.setattr(_search_tool, "retrieve_candidates", retrieve)
    monkeypatch.setattr(_search_tool, "relax_and_retry", lambda *a, **k: None)
    monkeypatch.setattr(_search_tool, "log_pipeline_result", lambda **k: None)

    _search_tool._run_search("light laptop for uni")

    assert calls == ["embed"]


def test_a_failed_prefetch_is_not_retried_inside_the_transaction(monkeypatch):
    monkeypatch.setattr(_retrieval, "_QUERY_CACHE", {})
    monkeypatch.setattr(_retrieval, "_FAILED_QUERIES", {})
    attempts = []

    def boom(_q):
        attempts.append(1)
        raise RuntimeError("embedding API down")

    monkeypatch.setattr(_retrieval, "embed_text", boom)

    _retrieval.prefetch_query_vector("q")  # never raises
    with pytest.raises(RuntimeError):
        _retrieval._get_query_vector("q")  # retrieve_candidates' call -> fallback

    assert attempts == [1]


# --------------------------------------------------------------------------
# reviews/processor.py
# --------------------------------------------------------------------------
def test_review_processor_first_chunk_holds_no_connection(db, monkeypatch):
    engine, session = db
    calls = []
    raw = SimpleNamespace(
        id=uuid.uuid4(), status=review_processor.ReviewStatus.MATCHED.value,
        video_id="vid", video_title="TUF F16 review", channel_id="ch",
        reprocess_requested_at=None,
        raw_transcript={"segments": [{"start": 0.0, "duration": 5.0, "text": "fast"}]},
    )
    link = SimpleNamespace(
        raw_review_id=raw.id, match_source="auto", family_id=uuid.uuid4(), laptop_id=uuid.uuid4(),
    )
    _reads(
        monkeypatch, session,
        [raw], [link],                        # the review, then its links
        [], [],                               # windows already saved: chunks, unattached
        [SimpleNamespace(channel_name="Chan")],
    )
    cands = review_processor.Candidates(
        ids={"F1": (link.family_id, None)}, link_config={link.family_id: link.laptop_id},
    )
    monkeypatch.setattr(review_processor, "build_candidates", lambda s, l: cands)
    monkeypatch.setattr(review_processor.time, "sleep", lambda s: None)
    monkeypatch.setattr(review_processor, "embed_text", _probe(engine, calls, "embed", [0.0] * 768))
    _FakeChatModel.probe = _probe(
        engine, calls, "llm",
        lambda: review_processor._WindowAnalysis(
            summary="fast", about="F1", sentiment="strength", topic="performance", scope="config",
        ),
    )
    monkeypatch.setattr(review_processor, "build_llm", lambda *a, **k: _FakeChatModel())

    report = review_processor.process_raw_review(uuid.uuid4(), session)

    assert calls == ["llm", "embed"]
    assert report["chunks_saved"] == 1


# --------------------------------------------------------------------------
# reviews/service.py ingest_for_laptop
# --------------------------------------------------------------------------
class _Guard:
    tripped = False
    attempted = 0

    def __init__(self, engine, calls):
        self.fetch = _probe(
            engine, calls, "transcript",
            lambda: _TranscriptResult(segments=[{"start": 0, "duration": 1, "text": "x"}]),
        )


def test_ingest_holds_no_connection_during_youtube_calls(db, monkeypatch):
    engine, session = db
    calls = []
    channel = SimpleNamespace(active=True, channel_id="ch")
    _reads(
        monkeypatch, session,
        ("TUF F16", "Asus"),  # laptop_row: (product_name, brand name) columns
        [channel],            # active channels
        [], [],               # `existing` lookup, one per video
    )
    monkeypatch.setattr(
        review_service, "discover_videos",
        _probe(engine, calls, "discover", lambda: [
            {"video_id": f"v{i}", "channel_id": "ch", "video_title": "TUF F16 review"}
            for i in range(2)
        ]),
    )
    monkeypatch.setattr(review_service, "match_laptop", lambda title, s: (None, 50.0))

    review_service.ingest_for_laptop(uuid.uuid4(), session, guard=_Guard(engine, calls))

    assert calls == ["discover", "transcript", "transcript"]


# --------------------------------------------------------------------------
# reviews/router.py
# --------------------------------------------------------------------------
def test_retry_transcripts_holds_no_connection_during_fetches(db, monkeypatch):
    engine, session = db
    calls = []
    rows = [
        SimpleNamespace(video_id=f"v{i}", transcript_attempts=0, raw_transcript={},
                        transcript_language=None, failure_reason=None, status="rejected")
        for i in range(2)
    ]
    _reads(monkeypatch, session, rows)
    monkeypatch.setattr(review_router, "TranscriptFetchGuard", lambda **k: _Guard(engine, calls))

    out = review_router.retry_transcripts(
        limit=20, delay_seconds=0, max_consecutive_blocks=3, session=session, _=None
    )

    assert calls == ["transcript", "transcript"]
    assert out["recovered"] == 2


def test_backfill_descriptions_holds_no_connection_during_videos_list(db, monkeypatch):
    engine, session = db
    calls = []
    rows = [SimpleNamespace(video_id="v1", video_description=None)]
    _reads(monkeypatch, session, rows, 0)
    monkeypatch.setattr(
        review_router, "fetch_descriptions", _probe(engine, calls, "videos.list", {"v1": "spec table"})
    )

    out = review_router.backfill_descriptions(limit=200, session=session, _=None)

    assert calls == ["videos.list"]
    assert out["filled"] == 1


# --------------------------------------------------------------------------
# users/router.py Google login avatar import
# --------------------------------------------------------------------------
def test_google_avatar_fetch_holds_no_connection(db, monkeypatch):
    engine, session = db
    calls = []
    _reads(monkeypatch, session, [])  # no avatar yet
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    monkeypatch.setattr(
        users_router.requests, "get",
        _probe(engine, calls, "avatar", lambda: SimpleNamespace(raise_for_status=lambda: None, content=png)),
    )

    users_router._import_google_avatar(session, SimpleNamespace(id=uuid.uuid4()), "https://x/a=s96-c")

    assert calls == ["avatar"]


# --------------------------------------------------------------------------
# laptops/pickscore_adapter.py cold build
# --------------------------------------------------------------------------
def test_cold_scoring_build_holds_no_connection(db, monkeypatch):
    engine, session = db
    calls = []

    def read(sess):
        sess.connection()
        return [], [], []

    monkeypatch.setattr(_pickscore_adapter, "_read", read)
    monkeypatch.setattr(_pickscore_adapter, "_build", _probe(engine, calls, "build", ({}, [], [])))
    monkeypatch.setattr(_pickscore_adapter.ranges_cache, "store", lambda *a: None)

    _pickscore_adapter.get_scoring_data(session, force_refresh=True)

    assert calls == ["build"]


# --------------------------------------------------------------------------
# Timeouts: a stalled provider must fail, not hang.
# --------------------------------------------------------------------------
@pytest.fixture
def hanging_server():
    """A TCP server that accepts connections and never sends a byte."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    held, stop = [], threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass

    threading.Thread(target=accept, daemon=True).start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    stop.set()
    for conn in held:
        conn.close()
    srv.close()


def test_agent_llm_times_out_against_a_hanging_provider(hanging_server, monkeypatch):
    from langchain_core.rate_limiters import InMemoryRateLimiter

    monkeypatch.setattr(llm_module, "settings", SimpleNamespace(
        gemini_api_key="test-key", llm_timeout_s=1.0, llm_max_retries=1, llm_total_deadline_s=60.0,
    ))
    monkeypatch.setattr(
        llm_module, "ChatGoogleGenerativeAI",
        functools.partial(llm_module.ChatGoogleGenerativeAI, base_url=hanging_server),
    )
    llm = agent_graph.build_agent_llm(
        rate_limiter=InMemoryRateLimiter(requests_per_second=100), model="google_genai:gemma-4-31b-it",
    )

    started = time.monotonic()
    with pytest.raises(Exception):
        llm.invoke("hello")
    assert time.monotonic() - started < 10


def test_embedder_times_out_against_a_hanging_provider(hanging_server, monkeypatch):
    monkeypatch.setattr(embeddings_service, "settings", SimpleNamespace(
        gemini_api_key="test-key", embedding_timeout_s=1.0, embedding_max_retries=1,
    ))
    monkeypatch.setattr(
        embeddings_service, "HttpOptions",
        functools.partial(embeddings_service.HttpOptions, base_url=hanging_server),
    )
    embedder = embeddings_service._build_embedder()

    started = time.monotonic()
    with pytest.raises(Exception):
        embedder.embed_query("hello")
    assert time.monotonic() - started < 10


def test_every_chat_model_construction_sets_timeout_and_retries():
    """Static guard for the next call site: no ChatGoogleGenerativeAI(...) in
    app/ may fall back to the library's timeout=None / max_retries=6 or skip
    the total deadline, and none may import the raw class, which has no
    deadline at all."""
    import ast
    import pathlib

    app_dir = pathlib.Path(review_processor.__file__).resolve().parents[1]
    missing = []
    for path in app_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ChatGoogleGenerativeAI"
            ):
                kws = {k.arg for k in node.keywords}
                if not {"timeout", "max_retries", "total_deadline_s"} <= kws:
                    missing.append(f"{path.name}:{node.lineno}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "langchain_google_genai"
                and any(a.name == "ChatGoogleGenerativeAI" for a in node.names)
                and path.name != "llm.py"
            ):
                missing.append(f"{path.name}:{node.lineno} imports the raw class")
    assert not missing, missing


# --------------------------------------------------------------------------
# Total deadline (app/common/llm.py): the bound that actually holds, since
# llm_timeout_s is a per-read limit. Each test sets the per-read timeout to
# 30 s and the deadline to 1 s, so only the deadline can be what fires.
# --------------------------------------------------------------------------
def _deadline_llm(monkeypatch, hanging_server):
    from langchain_core.rate_limiters import InMemoryRateLimiter

    monkeypatch.setattr(llm_module, "settings", SimpleNamespace(
        gemini_api_key="test-key", llm_timeout_s=30.0, llm_max_retries=1, llm_total_deadline_s=1.0,
    ))
    monkeypatch.setattr(
        llm_module, "ChatGoogleGenerativeAI",
        functools.partial(llm_module.ChatGoogleGenerativeAI, base_url=hanging_server),
    )
    return agent_graph.build_agent_llm(
        rate_limiter=InMemoryRateLimiter(requests_per_second=100), model="google_genai:gemma-4-31b-it",
    )


def test_the_deadline_not_the_read_timeout_ends_a_hanging_invoke(hanging_server, monkeypatch):
    LLMDeadlineExceeded = llm_module.LLMDeadlineExceeded

    llm = _deadline_llm(monkeypatch, hanging_server)
    started = time.monotonic()
    with pytest.raises(LLMDeadlineExceeded):
        llm.invoke("hello")
    assert time.monotonic() - started < 5


def test_the_deadline_ends_a_hanging_ainvoke(hanging_server, monkeypatch):
    import asyncio

    LLMDeadlineExceeded = llm_module.LLMDeadlineExceeded

    llm = _deadline_llm(monkeypatch, hanging_server)
    started = time.monotonic()
    with pytest.raises(LLMDeadlineExceeded):
        asyncio.run(llm.ainvoke("hello"))
    assert time.monotonic() - started < 5


def test_the_deadline_ends_a_stream_that_never_starts(hanging_server, monkeypatch):
    import asyncio

    LLMDeadlineExceeded = llm_module.LLMDeadlineExceeded

    llm = _deadline_llm(monkeypatch, hanging_server)

    async def consume():
        async for _ in llm.astream("hello"):
            pass

    started = time.monotonic()
    with pytest.raises(LLMDeadlineExceeded):
        asyncio.run(consume())
    assert time.monotonic() - started < 5


def test_a_stream_that_has_started_is_not_cut_off():
    """The deadline covers the first chunk only: once the user is seeing
    tokens, a slow later chunk must not abort the answer."""
    import asyncio

    LLMDeadlineExceeded = llm_module.LLMDeadlineExceeded
    with_first_chunk_deadline = llm_module.with_first_chunk_deadline

    async def slow_after_first():
        yield "first"
        await asyncio.sleep(0.6)
        yield "second"

    async def slow_first():
        await asyncio.sleep(0.6)
        yield "never"

    async def collect(gen):
        return [x async for x in with_first_chunk_deadline(gen, 0.2)]

    assert asyncio.run(collect(slow_after_first())) == ["first", "second"]
    with pytest.raises(LLMDeadlineExceeded):
        asyncio.run(collect(slow_first()))


def test_production_defaults_give_up_within_a_minute():
    """What a hanging provider costs in production: one attempt, 60 s total.
    (The mechanism is proven at 1 s above; this pins the numbers.)"""
    fields = Settings.model_fields
    assert fields["llm_total_deadline_s"].default == 60.0
    assert fields["llm_max_retries"].default == 1
    assert fields["embedding_timeout_s"].default == 15.0
    assert fields["embedding_max_retries"].default == 2
