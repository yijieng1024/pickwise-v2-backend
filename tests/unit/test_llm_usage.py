"""TokenUsageHandler sums usage across generations and skips calls that report none."""
import uuid

import pytest

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tests.unit._adapters import llm_usage_module

llm_usage = llm_usage_module()
TokenUsageHandler = llm_usage.TokenUsageHandler


def _result(*usages):
    gens = [
        ChatGeneration(message=AIMessage(content="x", usage_metadata=u) if u else AIMessage(content="x"))
        for u in usages
    ]
    return LLMResult(generations=[gens])


def test_handler_logs_summed_usage(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_usage, "log_llm_usage", lambda *a: calls.append(a))
    uid = uuid.uuid4()

    TokenUsageHandler("agent", "gemma-4-31b-it", uid).on_llm_end(_result(
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    ))

    assert calls == [("agent", "gemma-4-31b-it", 105, 21, uid, 0)]


def test_handler_skips_calls_without_usage(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_usage, "log_llm_usage", lambda *a: calls.append(a))

    TokenUsageHandler("review_chunk", "gemma-4-31b-it").on_llm_end(_result(None))

    assert calls == []


@pytest.mark.real_usage_logger
def test_a_failed_usage_write_logs_one_line_and_never_raises(monkeypatch):
    class _Broken:
        def __init__(self, *a, **kw):
            raise RuntimeError('password authentication failed for user "postgres"\nDETAIL: x')

    warnings = []
    monkeypatch.setattr(llm_usage, "Session", _Broken)
    monkeypatch.setattr(llm_usage.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))
    monkeypatch.setattr(llm_usage.logger, "exception",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no traceback")))

    # The whole handler path, as an LLM call's callback runs it.
    TokenUsageHandler("agent", "gemma-4-31b-it").on_llm_end(_result(
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    ))

    assert len(warnings) == 1 and "exc_info" not in warnings[0][1]
    assert "\n" not in (warnings[0][0][0] % warnings[0][0][1:])
