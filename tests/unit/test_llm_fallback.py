"""
Phase 2: primary -> ordered backups fallback, admin model switching, models.list.

The fallback tests run the REAL model class (app/common/llm.py) against a fake
Gemini server on loopback, so the errors are the ones google-genai and
langchain-google-genai actually raise for a 429 / 500 / 400 -- a stub raising
a hand-made exception would test the classifier against an error shape that
may not exist. Loopback only; no network, no key.
"""

import asyncio
import functools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from ._adapters import agent_graph, embeddings_service, llm_module, llm_config, app_settings_module

_P, _B = "google_genai:primary-model", "google_genai:backup-model"
_B2 = "google_genai:second-backup"


class _FakeGemini:
    """generateContent / streamGenerateContent per model, by behaviour:
    ok | 429 | 500 | 400 | hang | midstream_500. Records every request."""

    def __init__(self):
        self.behaviour: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []  # (model, method, body)
        self.list_pages: list[dict] = []

    def reply(self, model):
        return {
            "candidates": [{"content": {"parts": [{"text": f"answer from {model}"}], "role": "model"},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10},
        }

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # models.list
                page = 0
                if "pageToken=" in self.path:
                    page = int(self.path.split("pageToken=")[1].split("&")[0])
                fake.calls.append(("-", "models.list", {"page": page}))
                self._json(200, fake.list_pages[page])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                model = self.path.split("/models/")[1].split(":")[0]
                method = self.path.split(":")[1].split("?")[0]
                fake.calls.append((model, method, body))
                b = fake.behaviour[model]
                if b == "hang":
                    time.sleep(5)
                    return
                if b in ("429", "500", "400"):
                    status = {"429": "RESOURCE_EXHAUSTED", "500": "INTERNAL", "400": "INVALID_ARGUMENT"}[b]
                    self._json(int(b), {"error": {"code": int(b), "message": "fake", "status": status}})
                    return
                if method == "generateContent":
                    self._json(200, fake.reply(model))
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                first = fake.reply(model)
                first["candidates"][0]["content"]["parts"][0]["text"] = "first "
                self.wfile.write(f"data: {json.dumps(first)}\r\n\r\n".encode())
                self.wfile.flush()
                if b == "midstream_500":
                    err = {"error": {"code": 500, "message": "died mid-stream", "status": "INTERNAL"}}
                    self.wfile.write(f"data: {json.dumps(err)}\r\n\r\n".encode())
                    return
                self.wfile.write(f"data: {json.dumps(fake.reply(model))}\r\n\r\n".encode())

        return H

    def models(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def llm_settings(monkeypatch):
    """What building a model reads from settings. The unit tier runs with no
    configuration (no .env in CI), so a test that builds a real model must
    not reach the real Settings -- a local .env would hide that."""
    monkeypatch.setattr(llm_module, "settings", SimpleNamespace(
        gemini_api_key="test-key", llm_timeout_s=30.0, llm_max_retries=1, llm_total_deadline_s=1.0,
    ))


@pytest.fixture
def gemini(monkeypatch, llm_settings):
    fake = _FakeGemini()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(
        llm_module, "ChatGoogleGenerativeAI",
        functools.partial(llm_module.ChatGoogleGenerativeAI, base_url=url),
    )
    fake.url = url
    yield fake
    srv.shutdown()
    srv.server_close()


def _llm(backups=(_B,), **kw):
    return llm_module.build_llm("agent", temperature=0, models=(_P, backups), **kw)


def _stream(llm):
    async def go():
        return [c.content async for c in llm.astream("hi")]
    return asyncio.run(go())


# --------------------------------------------------------------------------
# Automatic fallback
# --------------------------------------------------------------------------
def test_a_429_goes_straight_to_the_backup(gemini):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})

    started = time.monotonic()
    reply = _llm().invoke("hi")

    assert reply.content == "answer from backup-model"
    # One attempt on the primary -- no retry, no backoff before switching.
    assert gemini.models() == ["primary-model", "backup-model"]
    assert time.monotonic() - started < 1.0


def test_a_primary_past_its_deadline_falls_back(gemini):
    gemini.behaviour.update({"primary-model": "hang", "backup-model": "ok"})

    started = time.monotonic()
    reply = asyncio.run(_llm().ainvoke("hi"))

    assert reply.content == "answer from backup-model"
    assert time.monotonic() - started < 4  # deadline is 1 s; the hang is 5 s


def test_a_server_error_falls_back(gemini):
    gemini.behaviour.update({"primary-model": "500", "backup-model": "ok"})
    assert _llm().invoke("hi").content == "answer from backup-model"


def test_a_bad_request_does_not_fall_back(gemini):
    # A 400 would fail on any model; asking the backup only spends its quota.
    gemini.behaviour.update({"primary-model": "400", "backup-model": "ok"})
    with pytest.raises(Exception) as exc:
        _llm().invoke("hi")
    assert not isinstance(exc.value, llm_module.LLMUnavailable)
    assert gemini.models() == ["primary-model"]


def test_a_stream_falls_back_before_its_first_chunk(gemini):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    chunks = _stream(_llm())
    assert "".join(chunks).startswith("first answer from backup-model")
    assert gemini.models() == ["primary-model", "backup-model"]


def test_no_model_switch_once_a_stream_has_started(gemini):
    # The primary's mid-stream error is a 500 -- fallback-eligible on its own
    # -- and the backup is healthy. It must still not be asked.
    gemini.behaviour.update({"primary-model": "midstream_500", "backup-model": "ok"})
    seen = []

    async def go():
        async for c in _llm().astream("hi"):
            seen.append(c.content)

    with pytest.raises(llm_module.LLMStreamInterrupted):
        asyncio.run(go())
    assert seen == ["first "]
    assert gemini.models() == ["primary-model"]


def test_both_models_failing_raises_the_backups_error(gemini):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "429"})
    with pytest.raises(llm_module.LLMUnavailable):
        _llm().invoke("hi")
    assert gemini.models() == ["primary-model", "backup-model"]


def test_structured_output_is_bound_on_both_models(gemini):
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    gemini.reply = lambda model: {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"answer": model})}], "role": "model"},
                        "finishReason": "STOP"}],
    }
    out = _llm().with_structured_output(Out).invoke("hi")
    assert out.answer == "backup-model"


# --------------------------------------------------------------------------
# A chain of three: primary -> backup -> second-backup, tried in order
# --------------------------------------------------------------------------
@pytest.mark.parametrize("failure", ["429", "500", "hang"])
def test_a_chain_falls_through_in_order(gemini, failure):
    gemini.behaviour.update({"primary-model": failure, "backup-model": failure, "second-backup": "ok"})

    reply = asyncio.run(_llm(backups=(_B, _B2)).ainvoke("hi"))

    assert reply.content == "answer from second-backup"
    assert gemini.models() == ["primary-model", "backup-model", "second-backup"]


def test_a_chain_stops_at_the_first_healthy_model(gemini):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok", "second-backup": "ok"})
    assert _llm(backups=(_B, _B2)).invoke("hi").content == "answer from backup-model"
    assert gemini.models() == ["primary-model", "backup-model"]


def test_a_bad_request_mid_chain_stops_the_chain(gemini):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "400", "second-backup": "ok"})
    with pytest.raises(Exception) as exc:
        _llm(backups=(_B, _B2)).invoke("hi")
    assert not isinstance(exc.value, llm_module.LLMUnavailable)
    assert gemini.models() == ["primary-model", "backup-model"]


def test_no_switch_after_a_backups_first_chunk(gemini):
    # The first backup starts streaming, then dies: the second is never asked.
    gemini.behaviour.update({"primary-model": "429", "backup-model": "midstream_500", "second-backup": "ok"})
    seen = []

    async def go():
        async for c in _llm(backups=(_B, _B2)).astream("hi"):
            seen.append(c.content)

    with pytest.raises(llm_module.LLMStreamInterrupted):
        asyncio.run(go())
    assert seen == ["first "]
    assert gemini.models() == ["primary-model", "backup-model"]


def test_usage_log_records_the_position_that_answered(gemini, monkeypatch):
    from ._adapters import llm_usage_module

    rows = []
    monkeypatch.setattr(llm_usage_module(), "log_llm_usage", lambda *a: rows.append(a))
    gemini.behaviour.update({"primary-model": "429", "backup-model": "500", "second-backup": "ok"})

    _llm(backups=(_B, _B2), feature="processor_extract").invoke("hi")

    assert rows == [("processor_extract", "second-backup", 7, 3, None, 2)]


# --------------------------------------------------------------------------
# Agent: tools on both models, SSE through astream_events, observability
# --------------------------------------------------------------------------
def _agent_events(gemini, monkeypatch, handler=None):
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    monkeypatch.setattr(agent_graph.llm_config, "resolve_models", lambda use: (_P, (_B,)))
    agent_graph._agent_rate_limiter.cache_clear()  # fresh per-model buckets, as in a new process
    agent = create_agent(agent_graph.build_agent_llm(), [lookup])
    tokens = []

    async def go():
        config = {"callbacks": [handler]} if handler else None
        async for e in agent.astream_events({"messages": [("user", "hi")]}, version="v2", config=config):
            if e["event"] == "on_chat_model_stream":
                tokens.append(e["data"]["chunk"].content)

    asyncio.run(go())
    return tokens


def test_agent_streams_the_backups_answer_with_tools_bound_on_both(gemini, monkeypatch):
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})

    started = time.monotonic()
    tokens = _agent_events(gemini, monkeypatch)

    # The backup's rate limiter starts full: its first call in a process does
    # not wait out the 5 s refill (InMemoryRateLimiter starts empty).
    assert time.monotonic() - started < 2
    assert "".join(tokens).startswith("first answer from backup-model")
    assert gemini.models() == ["primary-model", "backup-model"]
    for _model, _method, body in gemini.calls:
        names = [f["name"] for t in body.get("tools", []) for f in t.get("functionDeclarations", [])]
        assert names == ["lookup"], f"tools missing on {_model}"


def test_agent_stream_never_switches_after_its_first_token(gemini, monkeypatch):
    gemini.behaviour.update({"primary-model": "midstream_500", "backup-model": "ok"})
    with pytest.raises(llm_module.LLMStreamInterrupted):
        _agent_events(gemini, monkeypatch)
    assert gemini.models() == ["primary-model"]


def test_agent_run_log_records_the_answering_model_and_the_fallback(gemini, monkeypatch):
    from ._adapters import monitoring_service

    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    handler = monitoring_service.MonitoringCallbackHandler()

    _agent_events(gemini, monkeypatch, handler)

    assert (handler.model, handler.model_position) == ("backup-model", 1)
    assert handler.fallback_triggered is True
    assert handler.usage_by_model == {("backup-model", 1): [7, 3]}


def test_agent_run_log_without_fallback(gemini, monkeypatch):
    from ._adapters import monitoring_service

    gemini.behaviour.update({"primary-model": "ok", "backup-model": "ok"})
    handler = monitoring_service.MonitoringCallbackHandler()

    _agent_events(gemini, monkeypatch, handler)

    assert (handler.model, handler.model_position, handler.fallback_triggered) == ("primary-model", 0, False)


def test_usage_log_names_the_backup_when_it_answered(gemini, monkeypatch):
    from ._adapters import llm_usage_module

    rows = []
    monkeypatch.setattr(llm_usage_module(), "log_llm_usage", lambda *a: rows.append(a))
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})

    _llm(feature="agent").invoke("hi")

    assert rows == [("agent", "backup-model", 7, 3, None, 1)]


# --------------------------------------------------------------------------
# Where fallback must NOT apply
# --------------------------------------------------------------------------
def test_eval_pins_one_agent_model_with_no_fallback(llm_settings):
    llm = agent_graph.build_agent_llm(model=_P)
    assert isinstance(llm, llm_module.ChatGoogleGenerativeAI)  # not RunnableWithFallbacks
    assert llm.model == "primary-model"


@pytest.mark.parametrize("primary, judged_by", [("ok", "primary-model"), ("429", "backup-model")])
def test_the_eval_judge_falls_back_and_says_which_model_judged(gemini, monkeypatch, primary, judged_by):
    from ._adapters import run_eval

    monkeypatch.setattr(run_eval, "JUDGE_MODELS", (_P, (_B,)))
    monkeypatch.setattr(run_eval, "_judge_llm", None)
    monkeypatch.setattr(run_eval, "_get_rate_limiter", lambda: None)
    verdict = {"candidates": [{"content": {"parts": [{"text": '{"pass": true, "reason": "ok"}'}],
                                           "role": "model"}, "finishReason": "STOP"}]}
    monkeypatch.setattr(gemini, "reply", lambda model: verdict)
    gemini.behaviour.update({"primary-model": primary, "backup-model": "ok"})

    out = asyncio.run(run_eval.judge("q", "rubric", {"tool_calls": [], "text": "answer"}))
    assert out == {"passed": True, "reason": "ok", "judge_model": judged_by}


def test_eval_items_record_their_judge_models_and_the_summary_counts_them(monkeypatch, capsys):
    from ._adapters import run_eval

    async def agent(queries):
        return [{"text": "a", "tool_calls": [], "tool_outputs": []} for _ in queries]

    models = iter(["primary-model", "backup-model"])

    async def judge(query, rubric, result):
        return {"passed": True, "reason": "", "judge_model": next(models)}

    monkeypatch.setattr(run_eval, "call_agent_turns_with_retry", agent)
    monkeypatch.setattr(run_eval, "judge_with_retry", judge)
    monkeypatch.setattr(run_eval, "rule_checks", lambda *a: [{"name": "x", "passed": True}])
    item = {"id": "t", "category": "c", "turns": [
        {"query": "q1", "expect": {"rubric": "r"}}, {"query": "q2", "expect": {"rubric": "r"}},
    ]}
    r = asyncio.run(run_eval.run_one(item, use_judge=True))

    assert [t["judge_model"] for t in r["turns"]] == ["primary-model", "backup-model"]
    assert r["judge_model"] == ["backup-model", "primary-model"]
    assert "judge_model" not in r["turns"][0]["judge"]
    run_eval.print_summary([r])
    assert "Scored by judge model: backup-model 1, primary-model 1" in capsys.readouterr().out


def test_the_eval_token_budget_makes_the_third_6k_call_wait():
    from ._adapters import run_eval

    now, slept = [0.0], []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    budget = run_eval.TokenBudget(clock=lambda: now[0], sleep=sleep)  # 14.5K per 60 s

    async def three_calls():
        return [(await budget.acquire(6_000))[0] for _ in range(3)]

    assert asyncio.run(three_calls()) == [0.0, 0.0, 60.0]
    assert slept == [60.0]


def test_only_gemma_calls_are_charged_and_the_real_count_replaces_the_estimate(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from ._adapters import run_eval

    budget = run_eval.TokenBudget()
    monkeypatch.setattr(run_eval, "_token_budget", budget)
    calls = run_eval._model_calls()
    prompt = [[HumanMessage(content="x" * 2_000)]]

    async def call(run_id, model):
        await calls.on_chat_model_start({}, prompt, run_id=run_id, metadata={"ls_model_name": model})
        usage = {"input_tokens": 700, "output_tokens": 1, "total_tokens": 701}
        await calls.on_llm_end(LLMResult(generations=[[ChatGeneration(
            message=AIMessage(content="", usage_metadata=usage))]]), run_id=run_id)

    asyncio.run(call("lite", "gemini-3.5-flash-lite"))
    assert budget._spent == []
    asyncio.run(call("gemma", "models/gemma-4-31b-it"))
    assert [e[1] for e in budget._spent] == [700]  # estimated 1,001, then corrected
    assert calls.model == "gemma-4-31b-it"


def test_embeddings_have_no_fallback():
    import inspect

    src = inspect.getsource(embeddings_service)
    for marker in ("build_llm", "with_fallbacks", "llm_config"):
        assert marker not in src, f"embeddings must stay on one model ({marker})"


def test_no_backups_means_no_fallback(llm_settings):
    for backups in ((), (_P,)):  # a backup equal to the primary is skipped
        llm = llm_module.build_llm("agent", temperature=0, models=(_P, backups))
        assert isinstance(llm, llm_module.ChatGoogleGenerativeAI)


# --------------------------------------------------------------------------
# Admin switching: app_settings rows, env defaults, validation, cache
# --------------------------------------------------------------------------
_ENV = SimpleNamespace(**{
    f"llm_{use}_{slot}": f"google_genai:env-{use}-{slot.replace('_', '-')}"
    for use in ("agent", "review_processor", "processor")
    for slot in ("model", "backup_models")
})
_ENV.llm_agent_backup_models = "google_genai:env-agent-b1,google_genai:env-agent-b2"
_ENV.llm_processor_model = "google_genai:gemma-4-31b-it"
_ENV.llm_processor_backup_models = "google_genai:gemma-4-26b-a4b-it,google_genai:gemini-3.5-flash-lite"
_AGENT_ENV_BACKUPS = ("google_genai:env-agent-b1", "google_genai:env-agent-b2")


@pytest.fixture
def settings_db(monkeypatch, tmp_path):
    """llm_config on a real (SQLite) app_settings table."""
    from sqlmodel import Session, SQLModel, create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    SQLModel.metadata.create_all(engine, tables=[app_settings_module.AppSetting.__table__])
    monkeypatch.setattr(llm_config, "engine", engine)
    monkeypatch.setattr(llm_config, "settings", _ENV)
    monkeypatch.setattr(llm_config, "_cache", {})
    with Session(engine) as session:
        yield session


def test_env_defaults_apply_when_no_row_exists(settings_db):
    r = llm_config.resolve("agent")
    assert (r.primary.value, r.primary.source) == ("google_genai:env-agent-model", "env")
    assert (r.backups.values, r.backups.source) == (_AGENT_ENV_BACKUPS, "env")


def test_a_settings_change_takes_effect_without_a_restart(settings_db, llm_settings):
    assert llm_config.resolve_models("agent")[0] == "google_genai:env-agent-model"  # now cached

    app_settings_module.set_setting(settings_db, "agent.model", "google_genai:gemma-4-26b-a4b-it")
    assert llm_config.resolve_models("agent")[0] == "google_genai:gemma-4-26b-a4b-it"
    assert agent_graph.build_agent_llm().runnable.model == "gemma-4-26b-a4b-it"

    app_settings_module.delete_setting(settings_db, "agent.model")
    assert llm_config.resolve_models("agent")[0] == "google_genai:env-agent-model"

    app_settings_module.set_setting(settings_db, "agent.backup_models", '["google_genai:x-1"]')
    assert llm_config.resolve_models("agent")[1] == ("google_genai:x-1",)
    app_settings_module.set_setting(settings_db, "agent.backup_models", "[]")
    assert llm_config.resolve_models("agent")[1] == ()  # stored "no fallback"


def test_an_invalid_row_falls_back_to_the_env_default_and_says_why(settings_db):
    # One bad entry rejects the whole list, not just that entry.
    stored = '["google_genai:fine-model", "openai:gpt-5"]'
    settings_db.add(app_settings_module.AppSetting(key="agent.backup_models", value=stored))
    settings_db.add(app_settings_module.AppSetting(key="review_processor.model", value="Bad Model!"))
    settings_db.commit()

    agent = llm_config.resolve("agent")
    assert agent.backups.values == _AGENT_ENV_BACKUPS
    assert (agent.backups.source, agent.backups.stored) == ("env", stored)
    assert "backups[1]" in agent.backups.error and "Unsupported provider" in agent.backups.error
    rp = llm_config.resolve("review_processor")
    assert rp.primary.value == "google_genai:env-review_processor-model"
    assert "Malformed" in rp.primary.error


def test_a_db_failure_serves_env_defaults(settings_db, monkeypatch):
    def broken(use):
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_config, "_read_rows", broken)
    assert llm_config.resolve_models("agent") == ("google_genai:env-agent-model", _AGENT_ENV_BACKUPS)


def test_a_db_failure_is_cached_and_logged_once_without_a_traceback(settings_db, monkeypatch):
    calls, warnings = [], []

    def broken(use):
        calls.append(use)
        raise RuntimeError("password authentication failed\nDETAIL: more")

    monkeypatch.setattr(llm_config, "_read_rows", broken)
    monkeypatch.setattr(llm_config.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))
    monkeypatch.setattr(llm_config.logger, "exception",
                        lambda *a, **kw: pytest.fail("a settings outage must not log a traceback"))
    for _ in range(5):
        assert llm_config.resolve_models("agent")[0] == "google_genai:env-agent-model"
    assert calls == ["agent"]  # the failure is cached for the TTL like a success
    assert len(warnings) == 1 and "exc_info" not in warnings[0][1]
    assert "\n" not in (warnings[0][0][0] % warnings[0][0][1:])

    t = time.monotonic()
    monkeypatch.setattr(llm_config.time, "monotonic", lambda: t + llm_config._CACHE_TTL_S + 1)
    llm_config.resolve_models("agent")
    assert len(calls) == 2 and len(warnings) == 2  # next window retries, and says so once


def test_the_legacy_processor_row_is_read_as_is(settings_db):
    # processor.model predates this: bare model names, no provider prefix.
    app_settings_module.set_setting(settings_db, "processor.model", "gemma-4-26b-a4b-it")
    assert llm_config.resolve_models("processor")[0] == "google_genai:gemma-4-26b-a4b-it"


def test_processor_models_need_a_rate_limit_budget(settings_db):
    app_settings_module.set_setting(settings_db, "processor.model", "gemini-3.5-flash")
    r = llm_config.resolve("processor")
    assert r.primary.value == "google_genai:gemma-4-31b-it"
    assert "budget" in r.primary.error


@pytest.mark.parametrize("stored, error", [
    ('["google_genai:a-1", "google_genai:a-2", "google_genai:a-3"]', "at most 2 backups"),
    ('["google_genai:a-1", "a-1"]', "listed twice"),
    ('["google_genai:env-agent-model"]', "is the primary"),
    ('"google_genai:a-1"', "JSON array"),
    ("google_genai:a-1,google_genai:a-2", "not JSON"),
])
def test_invalid_stored_backup_lists_fall_back_to_env(settings_db, stored, error):
    app_settings_module.set_setting(settings_db, "agent.backup_models", stored)
    b = llm_config.resolve("agent").backups
    assert (b.values, b.source) == (_AGENT_ENV_BACKUPS, "env")
    assert error in b.error


def test_user_facing_uses_cap_backups_at_two_and_background_uses_do_not():
    many = [f"google_genai:m-{i}" for i in range(6)]
    assert len(llm_config.validate_backups("review_processor", many, "google_genai:p")) == 6
    assert llm_config.MAX_BACKUPS == {"agent": 2}  # the agent is the one user-facing use
    assert len(llm_config.validate_backups("agent", many[:2], "google_genai:p")) == 2
    with pytest.raises(ValueError, match="at most 2"):
        llm_config.validate_backups("agent", many[:3], "google_genai:p")


def test_processor_backups_each_need_a_budget():
    with pytest.raises(ValueError, match="backups\\[1\\].*budget"):
        llm_config.validate_backups(
            "processor", ["gemma-4-26b-a4b-it", "gemini-3.5-flash"], "google_genai:gemma-4-31b-it",
        )


def test_an_admin_primary_found_in_the_env_list_is_skipped(settings_db):
    # Env lists are checked against the env primary only; an admin primary
    # that happens to be in the env list is dropped from the chain.
    app_settings_module.set_setting(settings_db, "processor.model", "gemma-4-26b-a4b-it")
    assert llm_config.resolve_models("processor") == (
        "google_genai:gemma-4-26b-a4b-it", ("google_genai:gemini-3.5-flash-lite",),
    )


def test_env_backup_lists_are_validated_at_startup(monkeypatch):
    from ._adapters import Settings

    for key in ("DATABASE_URL", "SECRET_KEY", "BREVO_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("LLM_AGENT_BACKUP_MODELS", "a-1, a-2 ,a-3")
    with pytest.raises(Exception, match="at most 2 backups"):
        Settings(_env_file=None)
    monkeypatch.setenv("LLM_AGENT_BACKUP_MODELS", "a-1, a-2")
    assert Settings(_env_file=None).llm_agent_backup_models == "google_genai:a-1,google_genai:a-2"
    monkeypatch.setenv("LLM_AGENT_BACKUP_MODELS", "")
    assert Settings(_env_file=None).llm_agent_backup_models == ""
    # The recommendation use is gone: a leftover env var for it (still set on
    # an old deploy) is ignored rather than failing startup.
    monkeypatch.setenv("LLM_RECOMMENDATION_BACKUP_MODELS", "a-1,a-2,a-3")
    assert not hasattr(Settings(_env_file=None), "llm_recommendation_backup_models")


def test_the_default_env_chains_pass_validation(monkeypatch):
    # Every shipped default, including the agent's capped list, under the caps.
    from ._adapters import Settings

    for key in ("DATABASE_URL", "SECRET_KEY", "BREVO_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "x")
    for name in Settings.model_fields:
        if name.startswith("llm_") and name.endswith(("_model", "_backup_models")):
            monkeypatch.delenv(name.upper(), raising=False)
    s = Settings(_env_file=None)
    assert s.llm_agent_backup_models == "google_genai:gemma-4-26b-a4b-it"


def test_every_default_chain_model_is_in_the_allowed_list():
    # gemma-4-26b-it was retired while it sat in every chain: a rename has to
    # reach MODEL_OPTIONS (the list with rate limits) too, or this goes red.
    # The eval judge's chain is held to the same list.
    from ._adapters import Settings, run_eval

    fields = Settings.model_fields
    chains = {use: [fields[f"llm_{use}_model"].default,
                    *llm_config.split_env_list(fields[f"llm_{use}_backup_models"].default)]
              for use in llm_config.USES}
    chains["eval_judge"] = [run_eval.JUDGE_MODELS[0], *run_eval.JUDGE_MODELS[1]]
    for use, chain in chains.items():
        for spec in chain:
            assert llm_config.model_id(spec) in llm_config.MODEL_OPTIONS, (use, spec)


def test_the_processor_default_chain_is_the_pre_phase_2_one():
    from ._adapters import Settings

    fields = Settings.model_fields
    assert fields["llm_processor_model"].default == "google_genai:gemma-4-31b-it"
    assert llm_config.split_env_list(fields["llm_processor_backup_models"].default) == [
        "google_genai:gemma-4-26b-a4b-it", "google_genai:gemini-3.5-flash-lite", "google_genai:gemini-3.1-flash-lite",
    ]


@pytest.mark.parametrize("value, expected", [
    ("gemma-4-31b-it", "google_genai:gemma-4-31b-it"),
    ("google_genai:gemini-3.5-flash", "google_genai:gemini-3.5-flash"),
    ("google_genai:models/gemini-3.5-flash", "google_genai:gemini-3.5-flash"),
    (" google_genai : gemma-4-26b-a4b-it ", "google_genai:gemma-4-26b-a4b-it"),
])
def test_valid_specs_normalize(value, expected):
    assert llm_config.validate_spec("agent", value) == expected


@pytest.mark.parametrize("value", ["", "openai:gpt-5", "google_genai:", "google_genai:Gemma 4", "x:y:z"])
def test_invalid_specs_are_rejected(value):
    with pytest.raises(ValueError):
        llm_config.validate_spec("agent", value)


# --------------------------------------------------------------------------
# models.list
# --------------------------------------------------------------------------
_GEMMA_NO_DESCRIPTION = {
    "name": "models/gemma-4-31b-it", "displayName": "Gemma 4 31B",
    "inputTokenLimit": 131072, "outputTokenLimit": 8192,
    "supportedGenerationMethods": ["generateContent", "countTokens"],
}


def test_the_parser_tolerates_a_missing_description_and_filters_methods():
    pages = [{"models": [
        _GEMMA_NO_DESCRIPTION,
        {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3.5-flash", "displayName": "Gemini 3.5 Flash", "description": "fast",
         "inputTokenLimit": 1048576, "outputTokenLimit": 65536, "thinking": True,
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/bare", "supportedGenerationMethods": ["generateContent"]},
    ]}]

    models = llm_config.parse_model_list(pages)

    assert [m["name"] for m in models] == ["gemma-4-31b-it", "gemini-3.5-flash", "bare"]
    assert models[0] == {
        "name": "gemma-4-31b-it", "spec": "google_genai:gemma-4-31b-it", "display_name": "Gemma 4 31B",
        "input_token_limit": 131072, "output_token_limit": 8192, "thinking": None,
    }
    assert models[1]["thinking"] is True
    assert models[2]["display_name"] == "bare"


def test_model_list_follows_pagination_and_caches(gemini, monkeypatch):
    gemini.list_pages = [
        {"models": [_GEMMA_NO_DESCRIPTION], "nextPageToken": "1"},
        {"models": [{"name": "models/gemma-4-26b-a4b-it", "supportedGenerationMethods": ["generateContent"]}]},
    ]
    monkeypatch.setattr(llm_config, "_MODELS_URL", f"{gemini.url}/v1beta/models")
    monkeypatch.setattr(llm_config, "settings", SimpleNamespace(gemini_api_key="test-key"))
    monkeypatch.setattr(llm_config, "_model_list", None)

    models, _ = llm_config.model_list()
    assert [m["name"] for m in models] == ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]
    assert len(gemini.calls) == 2

    llm_config.model_list()
    assert len(gemini.calls) == 2  # cached
    llm_config.model_list(refresh=True)
    assert len(gemini.calls) == 4
