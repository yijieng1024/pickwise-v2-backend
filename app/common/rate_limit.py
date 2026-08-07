"""
Shared outbound pacing for the Gemma free tier.

Every Gemma call in this codebase competes for the same account quota, and the
binding limit is **tokens per minute, not requests per minute**. That
distinction is the whole reason this module exists: a `time.sleep()` between
records paces *requests* and says nothing about tokens, so a loop that looks
safely throttled can still sit far over the TPM ceiling and collect 429s.

The approach is the one `app/agent/graph.py` already uses for Pico: a
process-wide `InMemoryRateLimiter` attached to the LLM object itself, so every
call is paced automatically — including retries and any call site added later,
which a sleep in one loop can never cover.

What this module adds on top is the arithmetic. `build_gemma_limiter()` derives
the request rate from a token budget instead of taking a hand-picked delay:

    calls per minute = min(TPM budget / tokens per call, RPM ceiling)

so the number is auditable and moves correctly when a prompt grows.

**Size `tokens_per_call` from the realistic worst case, not the average.** The
limiter is request-based (it cannot see token counts), so the only way it can
honour a token budget is to assume every call is the largest one. That in turn
means callers must bound their prompts — an unbounded prompt makes any figure
here fiction. See `_MAX_SPEC_CHARS` in `app/processor/engine.py`.

One bucket per process: a multi-worker deploy multiplies the effective rate by
the worker count, the same caveat the agent carries.
"""
from langchain_core.rate_limiters import InMemoryRateLimiter

# Free-tier ceilings for gemma-4-31b-it.
GEMMA_TPM = 16_000
GEMMA_RPM = 15

# Rough chars-per-token for English prose and JSON. Only used to turn a
# character budget into a token estimate; being off by 20% is absorbed by
# sizing from the worst case rather than the average.
CHARS_PER_TOKEN = 4


def build_gemma_limiter(
    *,
    tokens_per_call: int,
    tpm_budget: int = GEMMA_TPM,
    rpm_ceiling: int = GEMMA_RPM,
) -> InMemoryRateLimiter:
    """
    A token bucket paced so `tokens_per_call`-sized calls stay inside
    `tpm_budget`, and never exceed `rpm_ceiling` requests per minute.

    Whichever limit binds first wins. For small prompts that is the RPM
    ceiling; for the processor's spec dumps it is comfortably the TPM budget.
    """
    if tokens_per_call <= 0:
        raise ValueError("tokens_per_call must be positive")

    calls_per_minute = min(tpm_budget / tokens_per_call, float(rpm_ceiling))

    return InMemoryRateLimiter(
        requests_per_second=calls_per_minute / 60,
        check_every_n_seconds=0.1,
        # No burst allowance: a bucket that can accumulate credit defeats the
        # point, since the TPM window is a rolling minute.
        max_bucket_size=1,
    )


def seconds_per_call(tokens_per_call: int, **kwargs) -> float:
    """
    The pacing `build_gemma_limiter` produces, in seconds between calls.

    Exposed so a route can quote an honest ETA in its 202 rather than
    hardcoding a number that drifts from the limiter it is describing.
    """
    limiter = build_gemma_limiter(tokens_per_call=tokens_per_call, **kwargs)
    return 1 / limiter.requests_per_second
