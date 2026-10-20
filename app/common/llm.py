"""
The chat model every feature builds: ChatGoogleGenerativeAI plus a wall-clock
deadline per LLM call.

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

Same class name on purpose: call sites change only their import line, and
tests that replace `module.ChatGoogleGenerativeAI` keep working.
"""
import asyncio
import concurrent.futures
import contextvars
from typing import Any, AsyncIterator, Optional

from langchain_google_genai import ChatGoogleGenerativeAI as _ChatGoogleGenerativeAI


class LLMDeadlineExceeded(TimeoutError):
    pass


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
        if self.total_deadline_s is None:
            return await super()._agenerate(*args, **kwargs)
        try:
            return await asyncio.wait_for(super()._agenerate(*args, **kwargs), self.total_deadline_s)
        except asyncio.TimeoutError:
            raise _deadline_error(self.total_deadline_s) from None

    async def _astream(self, *args: Any, **kwargs: Any):
        async for chunk in with_first_chunk_deadline(
            super()._astream(*args, **kwargs), self.total_deadline_s
        ):
            yield chunk
