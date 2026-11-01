"""
Sticky fallback within an agent turn (app/agent/graph.py::StickyFallback).

Run b6471466: the primary never succeeded, every step waited for it to fail
before a backup answered, and 5 steps took 242 s. These tests drive the real
run_agent / stream_agent against the fake Gemini server, so what they count is
the requests that actually reached each model.
"""

import asyncio
import contextlib

import pytest

from ._adapters import agent_graph, llm_module
from .test_llm_fallback import _B, _B2, _P, gemini, llm_settings  # noqa: F401 - fixtures

ROUTES = ["run_agent", "stream_agent"]


class _Sequence(dict):
    """behaviour[model] for a model whose behaviour changes between calls: each
    list entry serves one request, and the last one repeats."""

    def __getitem__(self, model):
        script = dict.__getitem__(self, model)
        return script.pop(0) if len(script) > 1 else script[0]


def _function_responses(body):
    return sum("functionResponse" in p for c in body["contents"] for p in c["parts"])


def _tool_steps(fake, n):
    """The model calls `lookup` for its first n steps, then answers in text.

    The fake's stream sends reply() twice per request and writes "first " over
    the first one, so a streamed tool step is a text chunk then the tool call.
    """
    text_reply, replies_per_request = fake.reply, {}

    def reply(model):
        request = len(fake.calls)
        replies_per_request[request] = replies_per_request.get(request, 0) + 1
        _model, method, body = fake.calls[-1]
        streams_text_first = method != "generateContent" and replies_per_request[request] == 1
        if _function_responses(body) >= n or streams_text_first:
            return text_reply(model)
        call = {"functionCall": {"name": "lookup", "args": {"q": f"step {_function_responses(body) + 1}"}}}
        return {"candidates": [{"content": {"parts": [call], "role": "model"}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10}}

    fake.reply = reply


@pytest.fixture
def turn(gemini, monkeypatch):  # noqa: F811
    """One user message through the production entry point."""
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return q

    monkeypatch.setattr(agent_graph, "_agent_tools", lambda user_id: [lookup])
    monkeypatch.setattr(agent_graph, "_build_agent_input", lambda *a: [HumanMessage(content="hi")])
    monkeypatch.setattr(agent_graph, "session_scope", lambda: contextlib.nullcontext())
    # The 5 s pacing between steps is real and irrelevant here.
    monkeypatch.setattr(agent_graph, "_agent_rate_limiter", lambda spec: None)

    def set_chain(*models):
        monkeypatch.setattr(agent_graph.llm_config, "resolve_models", lambda use: (models[0], models[1:]))

    def run(route):
        async def go():
            if route == "run_agent":
                return (await agent_graph.run_agent("hi", [], []))[0]
            reply = ""
            async for event in agent_graph.stream_agent("hi", [], []):
                if event["type"] == "final":
                    reply = event["reply"]
            return reply
        return asyncio.run(go())

    set_chain(_P, _B)
    run.chain = set_chain
    return run


@pytest.mark.parametrize("route", ROUTES)
def test_step_2_goes_straight_to_the_model_that_answered_step_1(gemini, turn, route):  # noqa: F811
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    _tool_steps(gemini, 1)

    reply = turn(route)

    assert reply.endswith("answer from backup-model")
    assert gemini.models() == ["primary-model", "backup-model", "backup-model"]
    # Sticky changes where a step starts, not what the model is given.
    assert _function_responses(gemini.calls[-1][2]) == 1


@pytest.mark.parametrize("route", ROUTES)
def test_a_new_turn_starts_at_the_primary_again(gemini, turn, route):  # noqa: F811
    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    _tool_steps(gemini, 1)
    turn(route)

    gemini.calls.clear()
    gemini.behaviour["primary-model"] = "ok"
    reply = turn(route)

    assert reply.endswith("answer from primary-model")
    assert gemini.models() == ["primary-model", "primary-model"]


def test_a_conversation_that_keeps_its_messages_still_resets_each_turn(gemini, turn):  # noqa: F811
    # The eval feeds turn 1's messages, backup's reply included, into turn 2.
    from langchain_core.messages import HumanMessage

    gemini.behaviour.update({"primary-model": "429", "backup-model": "ok"})
    agent = agent_graph.build_agent(agent_graph.build_agent_llm(), [])

    first = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="one")]}))
    gemini.calls.clear()
    gemini.behaviour["primary-model"] = "ok"
    asyncio.run(agent.ainvoke({"messages": [*first["messages"], HumanMessage(content="two")]}))

    assert gemini.models() == ["primary-model"]


@pytest.mark.parametrize("route", ROUTES)
def test_it_moves_down_when_the_sticky_model_fails_and_never_back_up(gemini, turn, route):  # noqa: F811
    turn.chain(_P, _B, _B2)
    gemini.behaviour = _Sequence({
        "primary-model": ["429"],
        "backup-model": ["ok", "429"],
        "second-backup": ["ok"],
    })
    _tool_steps(gemini, 2)

    assert turn(route).endswith("answer from second-backup")

    assert gemini.models() == [
        "primary-model", "backup-model",  # step 1
        "backup-model", "second-backup",  # step 2: starts at the backup, moves down
        "second-backup",                  # step 3: stays
    ]


def test_a_stream_on_the_sticky_model_never_switches_after_its_first_token(gemini, turn):  # noqa: F811
    turn.chain(_P, _B, _B2)
    gemini.behaviour = _Sequence({
        "primary-model": ["429"],
        "backup-model": ["ok", "midstream_500"],
        "second-backup": ["ok"],
    })
    _tool_steps(gemini, 1)

    with pytest.raises(llm_module.LLMStreamInterrupted):
        turn("stream_agent")

    assert gemini.models() == ["primary-model", "backup-model", "backup-model"]


def test_a_bad_request_on_the_sticky_model_ends_the_turn(gemini, turn):  # noqa: F811
    # A 400 is not fallback-eligible, wherever in the chain it lands.
    turn.chain(_P, _B, _B2)
    gemini.behaviour = _Sequence({
        "primary-model": ["429"],
        "backup-model": ["ok", "400"],
        "second-backup": ["ok"],
    })
    _tool_steps(gemini, 1)

    with pytest.raises(Exception) as exc:
        turn("run_agent")

    assert not isinstance(exc.value, llm_module.LLMUnavailable)
    assert gemini.models() == ["primary-model", "backup-model", "backup-model"]
