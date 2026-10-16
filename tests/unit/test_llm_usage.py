"""TokenUsageHandler sums usage across generations and skips calls that report none."""
import uuid

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

    TokenUsageHandler("recommendation", "gemini-3.5-flash", uid).on_llm_end(_result(
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    ))

    assert calls == [("recommendation", "gemini-3.5-flash", 105, 21, uid)]


def test_handler_skips_calls_without_usage(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_usage, "log_llm_usage", lambda *a: calls.append(a))

    TokenUsageHandler("review_chunk", "gemma-4-31b-it").on_llm_end(_result(None))

    assert calls == []
