"""
PickWise v2 — Minimal Eval Harness (v3)
========================================
Usage:
  python eval/run_eval.py run --label baseline              # full run (with judge)
  python eval/run_eval.py run --label quick --no-judge      # rule checks only, fast
  python eval/run_eval.py run --label x --only relevance_gating
  python eval/run_eval.py compare runs/A.jsonl runs/B.jsonl

Dependencies: pip install pyyaml google-genai (already in the project venv)
Judge API key: GEMINI_API_KEY from .env, like every other model

★ Run from the project root (app.config resolves .env against CWD):
  python eval/run_eval.py run --label baseline

★ Quota: agent + judge both run gemma-4-31b-it (judge backups: gemma-4-26b-a4b-it,
  gemini-3.5-flash-lite; free tier 15 RPM / 1500 RPD,
  and a strict 16,000 input-TPM cap). The TPM cap, not RPM, is the binding
  limit: judge prompts carry up to ~27k chars of tool output, so two judge
  calls in one minute can 429. All LLM calls share one global rate limiter
  (default 7 RPM — keep runs below 8 RPM; override with --rpm at your own
  risk). Each query ≈ 2-4 requests; a full 30-query run ≈ 20-30 minutes.
  Use --only / --limit for small slices.

v3 changes:
  - Budget rule now checks min(prices) only (fails only if the primary/cheapest
    recommendation exceeds budget; mentioning pricier alternatives is fine)
  - Judge retries 3x with backoff; a failed judge is recorded as unscored
    (passed=None) instead of crashing the whole run
  - Judge prompt forbids using its own (possibly stale) product knowledge;
    factual grounding is checked against raw tool outputs only
  - Summary shows unscored count; runs with unscored items must not be compared
  - JUDGE_MODEL set to gemini-2.5-flash

v4 changes:
  - Each entry now carries `status`: passed / failed / unscored / error.
    A missing or errored judge verdict can no longer surface as passed=true —
    rule checks alone never grant a pass when the judge was supposed to run
    (the 2026-07-18 baseline counted 3 judge-429 entries as passes this way).
    `passed` is kept for compatibility but is null for unscored/error entries.
  - Headline totals (summary + compare) count only scored entries; unscored
    and error entries are excluded from the denominator and reported separately.
  - compare reconstructs status for pre-v4 run files and ignores transitions
    into/out of unscored/error when flagging regressions/improvements.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).parent
RUNS_DIR = EVAL_DIR / "runs"
# The judge chain: primary, then backups on a 429 / 5xx / timeout, through the
# same build_llm fallback as the agent. A run judged partly by a backup is not
# comparable with one judged only by the primary, so every turn and item records
# its `judge_model` and the summary counts items per judge model.
JUDGE_MODELS = (
    "google_genai:gemma-4-31b-it",
    ("google_genai:gemma-4-26b-a4b-it", "google_genai:gemini-3.5-flash-lite"),
)
# The models' own default sampling temperature (models.get), which the judge
# ran at when it called the API without one.
JUDGE_TEMPERATURE = 1.0
# The agent model under test. None = the configured `agent` primary (app_settings
# or env), resolved once in cmd_run. Always ONE model with no fallback, so a run
# measures that model and not whichever answered. `--agent-model` overrides it.
AGENT_MODEL_SPEC = None

# make `import app` work when run as `python eval/run_eval.py`
sys.path.insert(0, str(EVAL_DIR.parent))

MAX_RPM = 7.0  # below 8 RPM to stay under the 16k input-TPM free-tier cap; override with --rpm


# ─────────────────────────────────────────────────────────────
# 0. Global rate limiter — shared by every agent LLM call and the judge
# ─────────────────────────────────────────────────────────────
_rate_limiter = None


def _get_rate_limiter():
    global _rate_limiter
    if _rate_limiter is None:
        from langchain_core.rate_limiters import InMemoryRateLimiter

        _rate_limiter = InMemoryRateLimiter(
            requests_per_second=MAX_RPM / 60 * 0.9,  # 10% headroom against 429s
            check_every_n_seconds=0.5,
            max_bucket_size=1,  # no bursts, strictly even pacing
        )
    return _rate_limiter


# The limiter above counts requests; Gemma's binding free-tier limit is 16K
# input tokens per minute, and at ~3K tokens per request 6.3 requests/min is
# ~19K (measured 2026-10-29, docs/pickwise-2.1-plan.md). So every Gemma call
# is also charged to a rolling 60 s token budget. Other models (flash-lite:
# 250K TPM) are not charged.
GEMMA_TOKENS_PER_WINDOW = 14_500
# Admission estimate from the prompt's characters, deliberately low: tool JSON
# measured ~2.0 chars/token, Chinese ~1.7, English prose ~3.2. Overcounting
# only delays the one call; the real input_tokens replace it once it returns.
_CHARS_PER_TOKEN_ESTIMATE = 2.0


class TokenBudget:
    """Input tokens spent per rolling window. `acquire` waits until the
    window has room, then records the call and returns its entry, a mutable
    [time, tokens] the caller may correct. A call bigger than the whole budget
    waits for an empty window and then goes, rather than never."""

    def __init__(self, budget: int = GEMMA_TOKENS_PER_WINDOW, window_s: float = 60.0,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.budget, self.window_s, self.clock, self.sleep = budget, window_s, clock, sleep
        self._spent: list[list] = []
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int) -> list:
        async with self._lock:
            while True:
                now = self.clock()
                self._spent = [e for e in self._spent if now - e[0] < self.window_s]
                if not self._spent or sum(e[1] for e in self._spent) + tokens <= self.budget:
                    entry = [now, tokens]
                    self._spent.append(entry)
                    return entry
                await self.sleep(self._spent[0][0] + self.window_s - now)


_token_budget = None


def _get_token_budget() -> TokenBudget:
    global _token_budget
    if _token_budget is None:
        _token_budget = TokenBudget()
    return _token_budget


def _model_calls():
    """A callback for one agent turn or judge call: charges each Gemma call
    to the token budget before it is sent, corrects the charge with the
    reported input tokens when it returns, and keeps the model of the last
    call started, which is the one that answered when the call succeeds."""
    from langchain_core.callbacks import AsyncCallbackHandler

    class ModelCalls(AsyncCallbackHandler):
        model = None

        def __init__(self):
            self._charged = {}

        async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
            self.model = ((metadata or {}).get("ls_model_name") or "unknown").removeprefix("models/")
            if self.model.startswith("gemma-"):
                chars = len(str(kwargs.get("invocation_params") or "")) + sum(
                    len(str(m.content)) + len(str(getattr(m, "tool_calls", "") or ""))
                    for batch in messages for m in batch
                )
                self._charged[run_id] = await _get_token_budget().acquire(
                    int(chars / _CHARS_PER_TOKEN_ESTIMATE) + 1
                )

        async def on_llm_end(self, response, *, run_id, **kwargs):
            entry = self._charged.pop(run_id, None)
            try:
                real = response.generations[0][0].message.usage_metadata["input_tokens"]
            except (AttributeError, IndexError, KeyError, TypeError):
                return  # no usage reported: the estimate stands
            if entry is not None and real:
                entry[1] = real

    return ModelCalls()


# ─────────────────────────────────────────────────────────────
# 1. Agent adapter (in-process)
# ─────────────────────────────────────────────────────────────
_agent = None  # compiled once, reused for the whole run


def _get_agent():
    global _agent
    if _agent is None:
        from langchain.agents import create_agent

        # the LLM is built by graph.py's factory so the eval always
        # measures the exact production configuration
        from app.agent.graph import build_agent_llm
        from app.agent.tools import ALL_TOOLS

        llm = build_agent_llm(rate_limiter=_get_rate_limiter(), model=AGENT_MODEL_SPEC)
        _agent = create_agent(llm, ALL_TOOLS)
    return _agent


def _content_to_text(content) -> str:
    """Gemini replies may arrive as a plain str or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
    return str(content)


async def call_agent_turns(queries: list[str]) -> list[dict]:
    """
    Multi-turn version: send each query in sequence, feeding the previous
    turn's full message list back in as the next turn's input — equivalent
    to a continuous POST /agent/chat conversation.

    Each turn reports only the tool_calls / tool_outputs it newly produced.
    Without the slice, turn 2 would still see turn 1's tool calls and any
    forbid_tools check would fire falsely.
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from app.agent.graph import _SYSTEM_PROMPT

    agent = _get_agent()
    messages = [SystemMessage(content=_SYSTEM_PROMPT)]
    turn_results = []

    for q in queries:
        before = len(messages)
        calls = _model_calls()
        result = await agent.ainvoke(
            {"messages": messages + [HumanMessage(content=q)]},
            config={"callbacks": [calls]},
        )
        messages = result["messages"]
        new = messages[before + 1:]  # skip the HumanMessage we just appended

        turn_results.append({
            "text": _content_to_text(messages[-1].content),
            "tool_calls": [
                tc["name"] for m in new
                for tc in (getattr(m, "tool_calls", None) or [])
            ],
            "tool_outputs": [str(m.content) for m in new if isinstance(m, ToolMessage)],
        })

    return turn_results

async def call_agent_turns_with_retry(queries: list[str], attempts: int = 3) -> list[dict]:
    """Transient 503/429 from the shared Gemini pool otherwise costs a whole
    eval item.

    Retries from turn 1, not from the failed turn: the conversation is
    stateful, so a half-finished message list cannot be resumed. Backoff is
    longer than the judge's (10/20s vs 5/10s) because one agent turn is several
    model calls — a 503 usually means the whole pool is congested, and retrying
    quickly just hits the same wall.
    """
    for i in range(attempts):
        try:
            return await call_agent_turns(queries)
        except Exception as e:
            # 429 RESOURCE_EXHAUSTED means the quota window is spent — retrying
            # inside seconds cannot succeed and just burns wall-clock time.
            # Only 503-class congestion is worth backing off for.
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                raise
            if i == attempts - 1:
                raise
            await asyncio.sleep(10 * (i + 1))

# ─────────────────────────────────────────────────────────────
# 2. Rule-based checks (deterministic, zero cost)
# ─────────────────────────────────────────────────────────────
PRICE_RE = re.compile(r"RM\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
# Grounding side: every number, not just RM-prefixed ones. The tool payloads
# carry bare floats ("price_rm": 4999.0, "price_min_rm": ...), so an RM-only
# scan of the tool outputs finds nothing and flags every correctly-cited
# price as invented.
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def extract_prices(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in PRICE_RE.findall(text)]


# ── RM figure grounding ────────────────────────────────────────────────
# Money figures the model states must trace back to something a tool actually
# returned. The model is fluent enough that an invented price range reads
# exactly like a real one, so this is the one hallucination class worth a
# deterministic check instead of leaving it to the judge.
#
# The segment regex also captures the tail of a range ("RM2,300 - RM2,500"),
# where only the first number carries the RM prefix.
_RM_SEGMENT = re.compile(r"RM\s?[\d,]+(?:\.\d+)?(?:\s*[-–~到至]\s*[\d,]+(?:\.\d+)?)?")
_NUMBER = re.compile(r"[\d,]+(?:\.\d+)?")

# Round figures below this are almost always the model reasoning about a delta
# ("add RM500 and you get...") rather than quoting a price it should ground.
_MIN_CHECKED_RM = 1000.0

# Absorbs rounding: tool returns 3399.0, response says "RM3,400".
_RM_TOLERANCE = 1.0


def _rm_figures(text: str) -> set[float]:
    """Every RM amount stated in the response, including range tails."""
    out: set[float] = set()
    for seg in _RM_SEGMENT.findall(text):
        for n in _NUMBER.findall(seg):
            try:
                out.add(float(n.replace(",", "")))
            except ValueError:
                pass
    return out


def _grounded_rm_figures(tool_outputs: list[str]) -> set[float]:
    """Every number appearing in the tool payloads.

    Deliberately over-inclusive — it matches bare numbers, not just RM-prefixed
    ones, because tool output is JSON ("price_rm": 3399.0) with no RM literal.
    A false negative only means we miss a fabrication; a false positive would
    flag an honest answer, which is the worse failure for a check meant to be
    trusted without a human reading every case.
    """
    out: set[float] = set()
    for o in tool_outputs:
        for n in _NUMBER.findall(o):
            try:
                out.add(float(n.replace(",", "")))
            except ValueError:
                pass
    return out


def _ungrounded_rm(
    response: str,
    tool_texts: list[str],
    user_texts: list[str],
) -> list[float]:
    """RM figures the response states that nothing supports.

    Ground truth is three things, all cumulative across the conversation:
      - tool output from this and every earlier turn (the agent legitimately
        cites turn-1 results when answering turn 2)
      - what the user themselves said (echoing back "your RM4,500 budget" is
        not a fabrication)
    """
    grounded = _grounded_rm_figures(tool_texts) | _grounded_rm_figures(user_texts)
    stated = {v for v in _rm_figures(response) if v >= _MIN_CHECKED_RM}
    return sorted(
        v for v in stated
        if not any(abs(v - g) <= _RM_TOLERANCE for g in grounded)
    )

def rule_checks(
    expect: dict,
    category: str,
    result: dict,
    tool_texts: list[str],
    user_texts: list[str],
) -> list[dict]:
    checks = []

    checks.append({
        "name": "non_empty_response",
        "passed": bool(result["text"] and result["text"].strip()),
        "detail": f"len={len(result['text'] or '')}",
    })

    if "tools" in expect:
        missing = [t for t in expect["tools"] if t not in result["tool_calls"]]
        checks.append({
            "name": "required_tools_called",
            "passed": not missing,
            "detail": f"missing={missing}, actual={result['tool_calls']}",
        })

    if "forbid_tools" in expect:
        violated = [t for t in expect["forbid_tools"] if t in result["tool_calls"]]
        checks.append({
            "name": "forbidden_tools_not_called",
            "passed": not violated,
            "detail": f"violated={violated}",
        })

    # Budget check (v3): only the CHEAPEST quoted price must be within budget,
    # i.e. the primary recommendation. Mentioning pricier alternatives or
    # market-price comparisons is normal behavior — its reasonableness is
    # left to the judge's rubric. constraint_relaxation queries legitimately
    # discuss over-budget numbers, so they are skipped.
    if "budget_max" in expect and category != "constraint_relaxation":
        prices = extract_prices(result["text"])
        limit = expect["budget_max"] * 1.05
        cheapest_over = bool(prices) and min(prices) > limit
        checks.append({
            "name": "primary_rec_within_budget",
            "passed": not cheapest_over,
            "detail": f"min_price={min(prices) if prices else None}, limit={limit}",
        })

    # Runs whenever any tool has produced output so far in the conversation —
    # a later turn can quote figures from an earlier turn's results.
    if tool_texts:
        ungrounded = _ungrounded_rm(result["text"], tool_texts, user_texts)
        checks.append({
            "name": "rm_figures_grounded",
            "passed": not ungrounded,
            "detail": f"ungrounded={ungrounded}",
        })

    return checks


# ─────────────────────────────────────────────────────────────
# 3. Semantic layer: LLM-as-judge
#    Key design: the judge must NOT use its own (possibly outdated) product
#    knowledge to decide truthfulness; factual grounding is checked only
#    against the raw tool outputs.
# ─────────────────────────────────────────────────────────────
JUDGE_PROMPT = """You are a strict QA reviewer. Evaluate whether a laptop \
recommendation agent's response satisfies the grading rubric.

IMPORTANT CONSTRAINT: the agent's product data (models, chips, prices, release \
dates) comes from a live, continuously updated database that is NEWER than your \
training knowledge. You must NOT grade based on your own beliefs about whether \
a product exists, whether a price is realistic, or whether a chip has been \
released.
Factual consistency must be checked ONLY against the TOOL OUTPUTS provided \
below: specific models, prices, and review quotes in the response should be \
traceable to the tool outputs; any specific factual claim with no basis in the \
tool outputs counts as fabrication.
Beyond that, evaluate only the behavioral quality of the response: does it \
address the user's specific needs, is the budget logic self-consistent, is the \
structure complete, is the language appropriate, and does it refuse when it \
should refuse.

User query:
{query}

Tools the agent called: {tool_calls}

Raw tool outputs (excerpt):
{tool_outputs}

Agent response:
{response}

Grading rubric:
{rubric}

Output ONLY JSON, no other text, in this format:
{{"pass": true or false, "reason": "one-sentence justification"}}
"""


_judge_llm = None


def _get_judge_llm():
    """Built once. Every model in the chain shares the global limiter (the
    quota pool the agent draws on) and production's timeout, single attempt
    and total deadline; judge_with_retry owns the retry loop."""
    global _judge_llm
    if _judge_llm is None:
        from app.common.llm import build_llm

        _judge_llm = build_llm(
            "eval_judge", temperature=JUDGE_TEMPERATURE, models=JUDGE_MODELS,
            rate_limiter_for=lambda _spec: _get_rate_limiter(),
        )
    return _judge_llm


async def judge(query: str, rubric: str, result: dict) -> dict:
    # Truncate each output individually, not the head of the joined blob —
    # otherwise evidence in later tool calls is invisible to the judge and
    # grounded responses get falsely flagged as fabrication. 4,500 per output
    # fits a full 5-result search_laptops payload (~3,400 chars now that each
    # result carries pick_score fields); at 3,000 the tail results' scores
    # were cut off and the judge flagged legitimately-cited PickScores as
    # fabricated.
    tool_outputs = "\n---\n".join(
        o[:4500] for o in result.get("tool_outputs", [])
    )[:27000] or "(no tool calls)"
    prompt = JUDGE_PROMPT.format(
        query=query,
        tool_calls=result["tool_calls"],
        tool_outputs=tool_outputs,
        response=result["text"][:6000],
        rubric=rubric,
    )
    calls = _model_calls()
    resp = await _get_judge_llm().ainvoke(prompt, config={"callbacks": [calls]})
    judge_model = calls.model
    raw = _content_to_text(resp.content).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
        return {"passed": bool(parsed["pass"]), "reason": parsed.get("reason", ""),
                "judge_model": judge_model}
    except (json.JSONDecodeError, KeyError):
        return {"passed": None, "reason": f"unparseable judge output: {raw[:200]}",
                "judge_model": judge_model}


async def judge_with_retry(query: str, rubric: str, result: dict, attempts: int = 3) -> dict:
    """Retry transient errors (e.g. 500) with backoff; if all attempts fail,
    record as unscored (passed=None) instead of crashing the run."""
    for i in range(attempts):
        try:
            return await judge(query, rubric, result)
        except Exception as e:
            if i == attempts - 1:
                return {"passed": None, "reason": f"judge_error: {type(e).__name__}: {e}"}
            await asyncio.sleep(5 * (i + 1))


# ─────────────────────────────────────────────────────────────
# 4. Run
# ─────────────────────────────────────────────────────────────
def _turns_of(item: dict) -> list[dict]:
    """Normalize a single-turn item into a one-turn `turns` list so run_one
    only ever handles one shape."""
    if "turns" in item:
        return item["turns"]
    return [{"query": item["query"], "expect": item.get("expect", {})}]


async def run_one(item: dict, use_judge: bool) -> dict:
    t0 = time.perf_counter()
    turns = _turns_of(item)

    try:
        results = await call_agent_turns_with_retry([t["query"] for t in turns])
        error = None
    except Exception as e:  # record agent crashes without aborting the run
        results = [{"text": "", "tool_calls": [], "tool_outputs": []} for _ in turns]
        error = f"{type(e).__name__}: {e}"
    latency = round(time.perf_counter() - t0, 2)

    turn_entries = []
    # Grounding accumulates across the conversation: a turn answered from the
    # existing shortlist pool calls no tool, so its own tool_outputs are empty
    # and the prices it cites were established earlier.
    tool_texts: list[str] = []
    user_texts: list[str] = []
    for turn, result in zip(turns, results):
        expect = turn.get("expect", {})
        tool_texts.extend(result["tool_outputs"])
        user_texts.append(turn["query"])
        checks = (
            rule_checks(expect, item["category"], result, tool_texts, user_texts)
            if not error else []
        )
        judge_result = None
        if use_judge and not error and "rubric" in expect:
            judge_result = await judge_with_retry(turn["query"], expect["rubric"], result)
        turn_entries.append({
            "query": turn["query"],
            "rule_checks": checks,
            # None when the judge did not run or every attempt failed.
            "judge_model": judge_result.pop("judge_model", None) if judge_result else None,
            "judge": judge_result,
            "tool_calls": result["tool_calls"],
            "tool_outputs": result["tool_outputs"],
            "response": result["text"],
        })

    # Aggregate: any failing turn fails the whole item. A conversation is a
    # single unit to the user — asking a good clarifying question in turn 1
    # and then dropping the thread in turn 2 is still a failure.
    all_checks = [c for te in turn_entries for c in te["rule_checks"]]
    verdicts = [te["judge"]["passed"] for te in turn_entries if te["judge"] is not None]

    if error:
        status = "error"
    elif not (all(c["passed"] for c in all_checks) if all_checks else False):
        status = "failed"  # deterministic rule failure stands even if a judge is unscored
    elif any(v is None for v in verdicts):
        status = "unscored"
    elif any(v is False for v in verdicts):
        status = "failed"
    else:
        status = "passed"

    last = turn_entries[-1]
    return {
        "id": item["id"],
        "category": item["category"],
        "lang": item.get("lang"),
        "query": " ⟶ ".join(t["query"] for t in turns),
        "status": status,
        "passed": {"passed": True, "failed": False}.get(status),  # None if unscored/error
        "unscored": status == "unscored",
        "turns": turn_entries,      # full per-turn detail
        # Every model that judged a turn: two names = partly judged by a backup.
        "judge_model": sorted({te["judge_model"] for te in turn_entries} - {None}),
        # Legacy fields below keep cmd_run / print_summary / cmd_compare working
        # unchanged. `judge` surfaces the first non-passing turn so the terminal
        # line points at the turn that actually broke; falls back to the last.
        "rule_checks": all_checks,
        "judge": next((te["judge"] for te in turn_entries
                       if te["judge"] and te["judge"]["passed"] is not True), last["judge"]),
        "latency_s": latency,
        "tool_calls": last["tool_calls"],
        "tool_outputs": last["tool_outputs"],
        "response": last["response"],
        "error": error,
    }

async def cmd_run(args):
    global MAX_RPM, AGENT_MODEL_SPEC
    if args.rpm:
        MAX_RPM = args.rpm
    from app.common import llm_config

    if args.agent_model:
        AGENT_MODEL_SPEC = llm_config.validate_spec(llm_config.AGENT, args.agent_model)
    else:
        AGENT_MODEL_SPEC = llm_config.resolve_models(llm_config.AGENT)[0]

    queries = yaml.safe_load((EVAL_DIR / "queries.yaml").read_text(encoding="utf-8"))
    if args.only:
        queries = [q for q in queries if q["category"] == args.only]
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        queries = [q for q in queries if q["id"] in wanted]
        unknown = wanted - {q["id"] for q in queries}
        if unknown:
            sys.exit(f"Unknown query ids: {sorted(unknown)}")
    if not queries:
        sys.exit(f"No queries match (category={args.only}, ids={args.ids})")
    if args.limit:
        queries = queries[: args.limit]

    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"{date.today().isoformat()}_{args.label}.jsonl"

    print(f"Running {len(queries)} queries | judge={'on' if not args.no_judge else 'off'} | → {out_path}\n")
    results = []
    for i, item in enumerate(queries, 1):  # serial: avoid hammering local DB / API quota
        r = await run_one(item, use_judge=not args.no_judge)
        results.append(r)
        mark = {"passed": "✅", "failed": "❌", "unscored": "⚠️ ", "error": "💥"}[r["status"]]
        why = r["error"] or ""
        failed_rules = [c["name"] for c in r["rule_checks"] if not c["passed"]]
        if failed_rules:
            why = f"rules: {failed_rules} {why}"
        if r["judge"] and r["judge"]["passed"] is False:
            why += f" judge: {r['judge']['reason']}"
        if r["unscored"]:
            why += " (judge unscored)"
        print(f"{mark} [{i:>2}/{len(queries)}] {r['id']:<16} {r['latency_s']:>6}s  {why}")

    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print_summary(results)
    print(f"\nSaved: {out_path}")


def print_summary(results: list[dict]):
    # Headline counts only entries with a real verdict — unscored/error entries
    # are excluded from the denominator and reported separately below.
    def tally(rs):
        scored = [r for r in rs if r["status"] in ("passed", "failed")]
        return sum(r["status"] == "passed" for r in scored), len(scored), len(rs) - len(scored)

    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    print(f"\n{'Category':<24}{'Passed':>8}")
    print("-" * 34)
    for cat, rs in sorted(by_cat.items()):
        p, n, excluded = tally(rs)
        note = f"  (+{excluded} unscored/error)" if excluded else ""
        print(f"{cat:<24}{p:>4}/{n}{note}")
    print("-" * 34)
    p, n, excluded = tally(results)
    note = f"  (+{excluded} unscored/error, excluded)" if excluded else ""
    print(f"{'TOTAL':<24}{p:>4}/{n}{note}")

    # Per-language breakdown: watch for language bias in retrieval/prompting
    by_lang = defaultdict(list)
    for r in results:
        if r.get("lang"):
            by_lang[r["lang"]].append(r)
    if by_lang:
        print("\nBy language:")
        for lang, rs in sorted(by_lang.items()):
            p, n, excluded = tally(rs)
            note = f"  (+{excluded} unscored/error)" if excluded else ""
            print(f"  {lang:<8}{p}/{n}{note}")

    # Items each judge model gave a verdict on; a run whose backup scored any
    # is not comparable with one scored only by the primary.
    judged = defaultdict(int)
    for r in results:
        for m in {t.get("judge_model") for t in r.get("turns", [])
                  if t.get("judge") and t["judge"]["passed"] is not None} - {None}:
            judged[m] += 1
    if judged:
        print("\nScored by judge model: " + ", ".join(f"{m} {n}" for m, n in sorted(judged.items())))

    unscored = sum(1 for r in results if r["status"] == "unscored")
    errors = sum(1 for r in results if r["status"] == "error")
    if unscored:
        print(f"\n⚠️ Judge unscored: {unscored} — this run must NOT be used for compare; re-run when the API is stable")
    if errors:
        print(f"⚠️ Agent errors: {errors} — see the error field of each entry")


# ─────────────────────────────────────────────────────────────
# 5. Compare
# ─────────────────────────────────────────────────────────────
def load_run(path: str) -> dict:
    return {
        json.loads(l)["id"]: json.loads(l)
        for l in Path(path).read_text(encoding="utf-8").splitlines()
        if l.strip()
    }


def entry_status(r: dict) -> str:
    """Status of an entry from either format: v4+ files carry an explicit
    `status`; older files are reconstructed from error/unscored/passed —
    critically, a pre-v4 unscored entry with passed=true is still unscored."""
    if "status" in r:
        return r["status"]
    if r.get("error"):
        return "error"
    if r.get("unscored"):
        return "unscored"
    return "passed" if r.get("passed") else "failed"


def cmd_compare(args):
    a, b = load_run(args.run_a), load_run(args.run_b)
    for name, run in [(args.run_a, a), (args.run_b, b)]:
        n = sum(1 for r in run.values() if entry_status(r) in ("unscored", "error"))
        if n:
            print(f"⚠️ {name} has {n} unscored/error entries — comparison is unreliable\n")

    ids = sorted(set(a) | set(b))
    name_a, name_b = Path(args.run_a).stem, Path(args.run_b).stem

    print(f"| id | category | {name_a} | {name_b} | Δ |")
    print("|---|---|---|---|---|")
    regressions, improvements = [], []
    for qid in ids:
        ra, rb = a.get(qid), b.get(qid)
        sa = entry_status(ra) if ra else None
        sb = entry_status(rb) if rb else None
        cat = (ra or rb)["category"]

        sym = {None: "—", "passed": "✅", "failed": "❌", "unscored": "⚠️", "error": "💥"}

        # Only a verdict-to-verdict flip counts as movement; transitions
        # into/out of unscored/error say nothing about agent quality.
        delta = ""
        if sa in ("passed", "failed") and sb in ("passed", "failed") and sa != sb:
            improved = sb == "passed"
            delta = "📈" if improved else "📉 REGRESSION"
            (improvements if improved else regressions).append(qid)
        print(f"| {qid} | {cat} | {sym[sa]} | {sym[sb]} | {delta} |")

    def tally(run):
        scored = [r for r in run.values() if entry_status(r) in ("passed", "failed")]
        return sum(entry_status(r) == "passed" for r in scored), len(scored)

    (pa, na), (pb, nb) = tally(a), tally(b)
    print(f"\nTotal (scored only): {name_a} {pa}/{na}  →  {name_b} {pb}/{nb}")
    if improvements:
        print(f"Improved: {improvements}")
    if regressions:
        print(f"⚠️ Regressions: {regressions}  ← inspect these responses before merging the change")


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="PickWise v2 eval harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run")
    pr.add_argument("--label", required=True, help="label for this run, e.g. baseline / after_rerank_fix")
    pr.add_argument("--only", help="run only one category")
    pr.add_argument("--ids", help="run only these query ids, comma-separated, e.g. relax_zh_003,clear_en_002")
    pr.add_argument("--limit", type=int, help="run at most N queries (small trial slices)")
    pr.add_argument("--rpm", type=float, help=f"override the global rate limit (default {MAX_RPM:.0f} RPM)")
    pr.add_argument("--no-judge", action="store_true", help="skip the LLM judge, rule checks only")
    pr.add_argument(
        "--agent-model",
        help="agent model under test, e.g. google_genai:gemma-4-26b-a4b-it "
        "(default: the configured agent primary). Never falls back.",
    )

    pc = sub.add_parser("compare")
    pc.add_argument("run_a")
    pc.add_argument("run_b")

    args = p.parse_args()
    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        cmd_compare(args)