"""
The chat model every feature builds: ChatGoogleGenerativeAI plus a wall-clock
deadline per LLM call, and the primary -> backup fallback wired around it.

WHY A DEADLINE ON TOP OF `timeout`: the library's `timeout` reaches httpx as a
per-READ limit (connect/read/write/pool each), not a total, so a provider that
trickles bytes, or retries inside the client, can keep one call alive far past
it. `total_deadline_s` bounds the whole call, across attempts:
  - non-streaming (invoke / ainvoke): the full call;
  - streaming (astream / astream_events, i.e. the agent's SSE): until the
    FIRST chunk arrives -- that chunk is what reaches the user, and a stream
    that has started is not cut off mid-answer.
The rate limiter's wait is not counted: langchain-core acquires it before
calling these methods, so the deadline measures the provider, not our queue.

FALLBACK (build_llm): `primary.with_fallbacks([backup])`, handling only
`LLMUnavailable`. The model class below raises that -- and only that -- for a
failure another model can fix: a 429, a 5xx, a transport error or timeout, the
deadline. Everything else (bad request, bad key, a malformed structured
output) raises as itself and does not fall back: a second model would fail the
same way, on more quota. A 429 is not retried on the primary first: the
attempt count is 1 (`llm_max_retries`), so the very first 429 is final there.

NO SWITCH AFTER THE FIRST CHUNK: once `_astream` has yielded a chunk, any
error is re-raised as `LLMStreamInterrupted`, which is not `LLMUnavailable`,
so `with_fallbacks` lets it through and the stream ends as an error. This
holds for every path that streams -- `astream`, and `ainvoke` under
`astream_events` (how create_agent drives the model in the SSE route), since
both go through `_astream`.
"""
import asyncio
import concurrent.futures
import contextvars
from typing import Any, AsyncIterator, Callable, Optional, Sequence

import httpx
from google.genai import errors as genai_errors
from langchain_core.rate_limiters import BaseRateLimiter
from langchain_core.runnables import Runnable
from langchain_google_genai import ChatGoogleGenerativeAI as _ChatGoogleGenerativeAI

from app.common import llm_config
from app.common.llm_usage import TokenUsageHandler
from app.config import settings

# On every model's runs: its position in the use's chain (0 = primary,
# 1..n = backups in order), so callbacks can tell which one answered.
_POSITION_TAG = "llm:position:"


def position_tag(position: int) -> str:
    return f"{_POSITION_TAG}{position}"


def position_from_tags(tags: Optional[Sequence[str]]) -> int:
    for t in tags or ():
        if t.startswith(_POSITION_TAG):
            return int(t[len(_POSITION_TAG):])
    return 0


class LLMUnavailable(Exception):
    """The model could not answer for a reason another model may not share:
    429, 5xx, timeout, network. The one exception the fallback handles."""


class LLMDeadlineExceeded(LLMUnavailable, TimeoutError):
    pass


class LLMStreamInterrupted(RuntimeError):
    """A stream failed after its first chunk. Never falls back: part of the
    answer has already been shown."""


def is_fallback_eligible(exc: Optional[BaseException]) -> bool:
    """429 / 5xx / transport error / timeout anywhere in the cause chain.
    langchain-google-genai wraps a ClientError in its own error type, so the
    status code is on the __cause__, not on the exception it raises."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (LLMUnavailable, httpx.TransportError, TimeoutError)):
            return True
        if isinstance(exc, genai_errors.APIError):
            code = getattr(exc, "code", None) or 0
            return code == 429 or code >= 500
        exc = exc.__cause__ or exc.__context__
    return False


def _classified(exc: Exception) -> Exception:
    """`exc` itself, or an LLMUnavailable carrying it when another model could
    succeed. The message keeps the original text, so keyword checks on it (the
    processor's retry wait) still see "429" / "503"."""
    if isinstance(exc, LLMUnavailable) or not is_fallback_eligible(exc):
        return exc
    unavailable = LLMUnavailable(f"{type(exc).__name__}: {exc}")
    unavailable.__cause__ = exc
    return unavailable


def _reraise_classified(exc: Exception):
    """Call from an except block. Re-raises `exc` untouched (cause chain
    intact) unless it is fallback-eligible, then raises LLMUnavailable."""
    classified = _classified(exc)
    if classified is exc:
        raise exc
    raise classified


def _deadline_error(seconds: float) -> LLMDeadlineExceeded:
    return LLMDeadlineExceeded(f"LLM call exceeded its {seconds:g}s total deadline")


async def with_first_chunk_deadline(stream: AsyncIterator, seconds: Optional[float]) -> AsyncIterator:
    """Yield from `stream`, raising if the first item takes longer than
    `seconds`. Later items are not timed."""
    if seconds is None:
        async for item in stream:
            yield item
        return
    try:
        first = await asyncio.wait_for(stream.__anext__(), seconds)
    except StopAsyncIteration:
        return
    except asyncio.TimeoutError:
        await stream.aclose()
        raise _deadline_error(seconds) from None
    yield first
    async for item in stream:
        yield item


class ChatGoogleGenerativeAI(_ChatGoogleGenerativeAI):
    total_deadline_s: Optional[float] = None

    def _generate(self, *args: Any, **kwargs: Any):
        try:
            return self._generate_within_deadline(*args, **kwargs)
        except Exception as e:
            _reraise_classified(e)

    def _generate_within_deadline(self, *args: Any, **kwargs: Any):
        if self.total_deadline_s is None:
            return super()._generate(*args, **kwargs)
        # A blocking httpx call cannot be interrupted in its own thread, so it
        # runs in a worker and the caller stops waiting at the deadline.
        # ponytail: the abandoned worker runs on until httpx's per-read timeout
        # (llm_timeout_s x attempts) ends it -- bounded, but it does hold a
        # thread and a provider request meanwhile.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ctx = contextvars.copy_context()
        future = executor.submit(ctx.run, super()._generate, *args, **kwargs)
        try:
            return future.result(timeout=self.total_deadline_s)
        except concurrent.futures.TimeoutError:
            raise _deadline_error(self.total_deadline_s) from None
        finally:
            executor.shutdown(wait=False)

    async def _agenerate(self, *args: Any, **kwargs: Any):
        try:
            if self.total_deadline_s is None:
                return await super()._agenerate(*args, **kwargs)
            try:
                return await asyncio.wait_for(super()._agenerate(*args, **kwargs), self.total_deadline_s)
            except asyncio.TimeoutError:
                raise _deadline_error(self.total_deadline_s) from None
        except Exception as e:
            _reraise_classified(e)

    async def _astream(self, *args: Any, **kwargs: Any):
        started = False
        try:
            async for chunk in with_first_chunk_deadline(
                super()._astream(*args, **kwargs), self.total_deadline_s
            ):
                started = True
                yield chunk
        except Exception as e:
            if started:
                raise LLMStreamInterrupted(f"stream failed after its first chunk: {e}") from e
            _reraise_classified(e)


def build_chat_model(
    spec: str,
    *,
    temperature: float,
    rate_limiter: Optional[BaseRateLimiter] = None,
    callbacks: Optional[list] = None,
    tags: Optional[list[str]] = None,
) -> ChatGoogleGenerativeAI:
    """One chat model from a 'provider:model' spec. Every model gets Phase 1's
    timeout, single attempt and total deadline, primary and backup alike.

    Not LangChain's `init_chat_model`: for google_genai that builds the stock
    ChatGoogleGenerativeAI, which has neither the total deadline nor the error
    classification fallback depends on. The spec is init_chat_model's own
    'provider:model' format, so a second provider is a branch here.
    """
    provider, model = llm_config.parse_spec(spec)
    if provider != "google_genai":
        raise ValueError(f"Unsupported provider {provider!r}")
    return ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        google_api_key=settings.gemini_api_key,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        total_deadline_s=settings.llm_total_deadline_s,
        rate_limiter=rate_limiter,
        callbacks=callbacks,
        tags=tags,
    )


def build_llm(
    use: str,
    *,
    temperature: float,
    feature: Optional[str] = None,
    user_id=None,
    rate_limiter_for: Optional[Callable[[str], BaseRateLimiter]] = None,
    models: Optional[tuple[str, Sequence[str]]] = None,
) -> Runnable:
    """The model chain for `use`: its configured primary, then each backup in
    order, moving on only on LLMUnavailable (429 / timeout / deadline / 5xx).

    `models` = (primary, backups) overrides the configured chain; empty
    backups means no fallback. `rate_limiter_for(spec)` gives each model its
    own limiter: free-tier quotas are per model, so a backup must not queue
    behind the bucket of the model that just 429'd. `feature` attaches a
    TokenUsageHandler per model, so each usage row names the model that
    answered and its position in the chain.

    `.bind_tools` / `.with_structured_output` on the result apply to every
    model in the chain (RunnableWithFallbacks forwards them).
    """
    primary, backups = models if models is not None else llm_config.resolve_models(use)
    chain = [primary, *(b for b in dict.fromkeys(backups) if b != primary)]

    def one(position: int, spec: str) -> ChatGoogleGenerativeAI:
        return build_chat_model(
            spec,
            temperature=temperature,
            rate_limiter=rate_limiter_for(spec) if rate_limiter_for else None,
            callbacks=(
                [TokenUsageHandler(feature, llm_config.model_id(spec), user_id, model_position=position)]
                if feature else None
            ),
            tags=[position_tag(position)],
        )

    llms = [one(i, spec) for i, spec in enumerate(chain)]
    if len(llms) == 1:
        return llms[0]
    return llms[0].with_fallbacks(llms[1:], exceptions_to_handle=(LLMUnavailable,))
