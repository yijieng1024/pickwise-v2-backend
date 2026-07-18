"""
PickWise v2 — Minimal Eval Harness (v3)
========================================
Usage:
  python eval/run_eval.py run --label baseline              # full run (with judge)
  python eval/run_eval.py run --label quick --no-judge      # rule checks only, fast
  python eval/run_eval.py run --label x --only relevance_gating
  python eval/run_eval.py compare runs/A.jsonl runs/B.jsonl

Dependencies: pip install pyyaml google-genai (already in the project venv)
Judge API key: GOOGLE_API_KEY env var if set, else GEMINI_API_KEY from .env

★ Run from the project root (app.config resolves .env against CWD):
  python eval/run_eval.py run --label baseline

★ Quota: agent + judge both run gemma-4-31b-it (free tier 15 RPM / 1500 RPD,
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
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).parent
RUNS_DIR = EVAL_DIR / "runs"
JUDGE_MODEL = "gemma-4-31b-it"  # same model as the agent — one shared quota pool

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

        llm = build_agent_llm(rate_limiter=_get_rate_limiter())
        _agent = create_agent(llm, ALL_TOOLS)
    return _agent


def _content_to_text(content) -> str:
    """Gemini replies may arrive as a plain str or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
    return str(content)


async def call_agent(query: str) -> dict:
    """
    In-process call to the LangGraph agent — equivalent to a first-turn
    POST /agent/chat (no history, no shortlist pool). Built here instead of
    reusing app.agent.graph.run_agent() because that only returns
    (text, results) and exposes neither tool-call names nor raw tool outputs.
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    from app.agent.graph import _SYSTEM_PROMPT

    agent = _get_agent()
    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=query),
            ]
        }
    )
    messages = result["messages"]
    text = _content_to_text(messages[-1].content)
    tool_calls = [
        tc["name"]
        for m in messages
        for tc in (getattr(m, "tool_calls", None) or [])
    ]
    tool_outputs = [str(m.content) for m in messages if isinstance(m, ToolMessage)]
    return {"text": text, "tool_calls": tool_calls, "tool_outputs": tool_outputs}


# ─────────────────────────────────────────────────────────────
# 2. Rule-based checks (deterministic, zero cost)
# ─────────────────────────────────────────────────────────────
PRICE_RE = re.compile(r"RM\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def extract_prices(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in PRICE_RE.findall(text)]


def rule_checks(item: dict, result: dict) -> list[dict]:
    expect = item.get("expect", {})
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
    if "budget_max" in expect and item["category"] != "constraint_relaxation":
        prices = extract_prices(result["text"])
        limit = expect["budget_max"] * 1.05
        cheapest_over = bool(prices) and min(prices) > limit
        checks.append({
            "name": "primary_rec_within_budget",
            "passed": not cheapest_over,
            "detail": f"min_price={min(prices) if prices else None}, limit={limit}",
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


def _judge_api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    from app.config import settings

    return settings.gemini_api_key


async def judge(item: dict, result: dict) -> dict:
    from google import genai

    await _get_rate_limiter().aacquire()  # shares the quota pool with the agent
    client = genai.Client(api_key=_judge_api_key())
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
        query=item["query"],
        tool_calls=result["tool_calls"],
        tool_outputs=tool_outputs,
        response=result["text"][:6000],
        rubric=item["expect"]["rubric"],
    )
    resp = await client.aio.models.generate_content(model=JUDGE_MODEL, contents=prompt)
    raw = resp.text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
        return {"passed": bool(parsed["pass"]), "reason": parsed.get("reason", "")}
    except (json.JSONDecodeError, KeyError):
        return {"passed": None, "reason": f"unparseable judge output: {raw[:200]}"}


async def judge_with_retry(item: dict, result: dict, attempts: int = 3) -> dict:
    """Retry transient errors (e.g. 500) with backoff; if all attempts fail,
    record as unscored (passed=None) instead of crashing the run."""
    for i in range(attempts):
        try:
            return await judge(item, result)
        except Exception as e:
            if i == attempts - 1:
                return {"passed": None, "reason": f"judge_error: {type(e).__name__}: {e}"}
            await asyncio.sleep(5 * (i + 1))


# ─────────────────────────────────────────────────────────────
# 4. Run
# ─────────────────────────────────────────────────────────────
async def run_one(item: dict, use_judge: bool) -> dict:
    t0 = time.perf_counter()
    try:
        result = await call_agent(item["query"])
        result.setdefault("tool_outputs", [])
        error = None
    except Exception as e:  # record agent crashes without aborting the run
        result = {"text": "", "tool_calls": [], "tool_outputs": []}
        error = f"{type(e).__name__}: {e}"
    latency = round(time.perf_counter() - t0, 2)

    checks = rule_checks(item, result) if not error else []
    judge_result = None
    if use_judge and not error and "rubric" in item.get("expect", {}):
        judge_result = await judge_with_retry(item, result)

    rule_pass = all(c["passed"] for c in checks) if checks else False
    judge_pass = judge_result["passed"] if judge_result else None  # None = unscored
    # `status` is the single source of truth. An entry with no real verdict
    # (agent error, or judge attempted but never returned) must never surface
    # as passed — it gets passed=None and is excluded from headline counts.
    if error:
        status = "error"
    elif not rule_pass:
        status = "failed"  # deterministic rule failure stands even if the judge is unscored
    elif judge_result is not None and judge_pass is None:
        status = "unscored"
    else:
        status = "passed" if judge_pass is not False else "failed"

    return {
        "id": item["id"],
        "category": item["category"],
        "lang": item.get("lang"),
        "query": item["query"],
        "status": status,
        "passed": {"passed": True, "failed": False}.get(status),  # None if unscored/error
        "unscored": status == "unscored",
        "rule_checks": checks,
        "judge": judge_result,
        "latency_s": latency,
        "tool_calls": result["tool_calls"],
        "tool_outputs": result["tool_outputs"],
        "response": result["text"],
        "error": error,
    }


async def cmd_run(args):
    global MAX_RPM
    if args.rpm:
        MAX_RPM = args.rpm

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

    pc = sub.add_parser("compare")
    pc.add_argument("run_a")
    pc.add_argument("run_b")

    args = p.parse_args()
    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        cmd_compare(args)