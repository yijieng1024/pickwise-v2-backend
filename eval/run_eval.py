"""
PickWise v2 — Minimal Eval Harness
===================================
用法:
  python run_eval.py run --label baseline          # 跑全部 25 条,存 runs/<date>_baseline.jsonl
  python run_eval.py run --label baseline --only relevance_gating   # 只跑某类
  python run_eval.py run --label quick --no-judge  # 只跑规则层,不花 judge 的钱
  python run_eval.py compare runs/A.jsonl runs/B.jsonl              # 两次 run 对比表

依赖: pip install pyyaml google-genai(项目 venv 已有)
judge API key: 优先 GOOGLE_API_KEY 环境变量,否则用 .env 里的 GEMINI_API_KEY

★ 必须从项目根目录运行(app.config 按 CWD 找 .env):
  python eval/run_eval.py run --label baseline

★ API 配额:gemma-4-31b-it 免费层 15 RPM / 1500 RPD(agent 和 judge 都是它)。
  - 所有 agent LLM 调用和 judge 调用共用一个全局限速器(默认 15 RPM,--rpm 可调),
    每次 LLM 请求间隔 ~4.5s,单条 query 会比不限速时慢,这是正常的。
  - 每条 query ≈ 2-4 个请求(ReAct 循环 1-3 次 + judge 1 次)。全量 30 条
    ≈ 60-120 个请求,RPD 充裕,一次跑完约 10-15 分钟。
  - 需要小批量试跑时用 --only / --limit 切片,之后拿 compare 拼对比。
    例: python eval/run_eval.py run --label day1 --only clear_intent --limit 5
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
JUDGE_MODEL = "gemma-4-31b-it"  # 便宜、够用;换成你在用的 flash 版本

# 让 `import app` 在 `python eval/run_eval.py` 下可用
sys.path.insert(0, str(EVAL_DIR.parent))

MAX_RPM = 15.0  # gemma-4-31b-it 免费层限制;--rpm 可覆盖


# ─────────────────────────────────────────────────────────────
# 0. 全局限速 —— agent 的每次 LLM 调用 + judge 调用共用一个桶
# ─────────────────────────────────────────────────────────────
_rate_limiter = None


def _get_rate_limiter():
    global _rate_limiter
    if _rate_limiter is None:
        from langchain_core.rate_limiters import InMemoryRateLimiter

        _rate_limiter = InMemoryRateLimiter(
            requests_per_second=MAX_RPM / 60 * 0.9,  # 留 10% 余量防 429
            check_every_n_seconds=0.5,
            max_bucket_size=1,  # 不允许突发,严格匀速
        )
    return _rate_limiter


# ─────────────────────────────────────────────────────────────
# 1. Agent 适配层
# ─────────────────────────────────────────────────────────────
_agent = None  # 编译一次,整个 run 复用


def _get_agent():
    global _agent
    if _agent is None:
        from langchain.agents import create_agent
        from langchain_google_genai import ChatGoogleGenerativeAI

        # model/temperature 从 graph.py 导入,保证评的就是线上那套配置
        from app.agent.graph import AGENT_MODEL, AGENT_TEMPERATURE
        from app.agent.tools import ALL_TOOLS
        from app.config import settings

        llm = ChatGoogleGenerativeAI(
            model=AGENT_MODEL,
            temperature=AGENT_TEMPERATURE,
            google_api_key=settings.gemini_api_key,
            rate_limiter=_get_rate_limiter(),
        )
        _agent = create_agent(llm, ALL_TOOLS)
    return _agent


def _content_to_text(content) -> str:
    """Gemini 的回复 content 可能是 str 或 parts list。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p if isinstance(p, str) else p.get("text", "")
            for p in content
        )
    return str(content)


async def call_agent(query: str) -> dict:
    """
    In-process 调用 LangGraph agent(等价于 POST /agent/chat 的单轮、无历史、
    无 shortlist pool 场景)。返回:
      {"text": 最终回复文本, "tool_calls": ["search_laptops", ...]}

    说明:不复用 app.agent.graph.run_agent() 是因为它只返回 (text, results),
    不暴露 tool 调用轨迹;这里用同一份 ALL_TOOLS + _SYSTEM_PROMPT 自行组图。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

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
    return {"text": text, "tool_calls": tool_calls}


# ─────────────────────────────────────────────────────────────
# 2. 规则层检查(确定性、零成本)
# ─────────────────────────────────────────────────────────────
PRICE_RE = re.compile(r"RM\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def extract_prices(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in PRICE_RE.findall(text)]


def rule_checks(item: dict, result: dict) -> list[dict]:
    """返回 [{"name":..., "passed":..., "detail":...}]"""
    expect = item.get("expect", {})
    checks = []

    # 非空回复
    checks.append({
        "name": "non_empty_response",
        "passed": bool(result["text"] and result["text"].strip()),
        "detail": f"len={len(result['text'] or '')}",
    })

    # 必须调用的 tools(子集匹配)
    if "tools" in expect:
        missing = [t for t in expect["tools"] if t not in result["tool_calls"]]
        checks.append({
            "name": "required_tools_called",
            "passed": not missing,
            "detail": f"missing={missing}, actual={result['tool_calls']}",
        })

    # 禁止调用的 tools
    if "forbid_tools" in expect:
        violated = [t for t in expect["forbid_tools"] if t in result["tool_calls"]]
        checks.append({
            "name": "forbidden_tools_not_called",
            "passed": not violated,
            "detail": f"violated={violated}",
        })

    # 预算检查:回复里出现的价格不应超预算(容忍 5%,因为可能提到原价/对比价)
    # 注意:这是启发式,constraint_relaxation 类会合理提到超预算数字,故跳过该类
    if "budget_max" in expect and item["category"] != "constraint_relaxation":
        prices = extract_prices(result["text"])
        limit = expect["budget_max"] * 1.05
        over = [p for p in prices if p > limit]
        checks.append({
            "name": "recommendations_within_budget",
            "passed": not over,
            "detail": f"over_budget_prices={over}" if over else f"prices={prices}",
        })

    return checks


# ─────────────────────────────────────────────────────────────
# 3. 语义层:LLM-as-judge
# ─────────────────────────────────────────────────────────────
JUDGE_PROMPT = """你是一个严格的 QA 评审。评估一个笔电推荐 agent 的回复是否满足判分标准。

用户问题:
{query}

Agent 调用过的工具: {tool_calls}

Agent 回复:
{response}

判分标准(rubric):
{rubric}

只输出 JSON,无其他文字,格式:
{{"pass": true 或 false, "reason": "一句话理由"}}
"""


def _judge_api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    from app.config import settings

    return settings.gemini_api_key


async def judge(item: dict, result: dict) -> dict:
    from google import genai

    await _get_rate_limiter().aacquire()  # 与 agent 的 LLM 调用共用配额
    client = genai.Client(api_key=_judge_api_key())
    prompt = JUDGE_PROMPT.format(
        query=item["query"],
        tool_calls=result["tool_calls"],
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
        return {"passed": False, "reason": f"judge 输出无法解析: {raw[:200]}"}


# ─────────────────────────────────────────────────────────────
# 4. Run
# ─────────────────────────────────────────────────────────────
async def run_one(item: dict, use_judge: bool) -> dict:
    t0 = time.perf_counter()
    try:
        result = await call_agent(item["query"])
        error = None
    except Exception as e:  # agent 崩了也要记录,不中断整个 run
        result = {"text": "", "tool_calls": []}
        error = f"{type(e).__name__}: {e}"
    latency = round(time.perf_counter() - t0, 2)

    checks = rule_checks(item, result) if not error else []
    judge_result = None
    if use_judge and not error and "rubric" in item.get("expect", {}):
        judge_result = await judge(item, result)

    rule_pass = all(c["passed"] for c in checks) if checks else False
    judge_pass = judge_result["passed"] if judge_result else None
    overall = (not error) and rule_pass and (judge_pass is not False)

    return {
        "id": item["id"],
        "category": item["category"],
        "query": item["query"],
        "passed": overall,
        "rule_checks": checks,
        "judge": judge_result,
        "latency_s": latency,
        "tool_calls": result["tool_calls"],
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
    if not queries:
        sys.exit(f"没有匹配的 query (category={args.only})")
    if args.limit:
        queries = queries[: args.limit]

    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"{date.today().isoformat()}_{args.label}.jsonl"

    print(f"跑 {len(queries)} 条 | judge={'on' if not args.no_judge else 'off'} | → {out_path}\n")
    results = []
    for i, item in enumerate(queries, 1):  # 串行跑,避免打爆本地 DB / API 配额
        r = await run_one(item, use_judge=not args.no_judge)
        results.append(r)
        mark = "✅" if r["passed"] else "❌"
        why = r["error"] or (r["judge"]["reason"] if r["judge"] and not r["judge"]["passed"] else "")
        failed_rules = [c["name"] for c in r["rule_checks"] if not c["passed"]]
        if failed_rules:
            why = f"rules: {failed_rules} {why}"
        print(f"{mark} [{i:>2}/{len(queries)}] {r['id']:<12} {r['latency_s']:>5}s  {why}")

    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print_summary(results)
    print(f"\n已保存: {out_path}")


def print_summary(results: list[dict]):
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["passed"])
    total = sum(r["passed"] for r in results)
    print(f"\n{'类别':<24}{'通过':>8}")
    print("-" * 34)
    for cat, flags in sorted(by_cat.items()):
        print(f"{cat:<24}{sum(flags):>4}/{len(flags)}")
    print("-" * 34)
    print(f"{'TOTAL':<24}{total:>4}/{len(results)}")


# ─────────────────────────────────────────────────────────────
# 5. Compare
# ─────────────────────────────────────────────────────────────
def load_run(path: str) -> dict:
    return {json.loads(l)["id"]: json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()}


def cmd_compare(args):
    a, b = load_run(args.run_a), load_run(args.run_b)
    ids = sorted(set(a) | set(b))
    name_a, name_b = Path(args.run_a).stem, Path(args.run_b).stem

    print(f"\n| id | category | {name_a} | {name_b} | Δ |")
    print("|---|---|---|---|---|")
    regressions, improvements = [], []
    for qid in ids:
        pa = a.get(qid, {}).get("passed")
        pb = b.get(qid, {}).get("passed")
        cat = (a.get(qid) or b.get(qid))["category"]
        sym = lambda p: "✅" if p else ("❌" if p is not None else "—")
        delta = ""
        if pa is not None and pb is not None and pa != pb:
            delta = "📈" if pb else "📉 REGRESSION"
            (improvements if pb else regressions).append(qid)
        print(f"| {qid} | {cat} | {sym(pa)} | {sym(pb)} | {delta} |")

    ta = sum(r["passed"] for r in a.values())
    tb = sum(r["passed"] for r in b.values())
    print(f"\n总分: {name_a} {ta}/{len(a)}  →  {name_b} {tb}/{len(b)}")
    if improvements:
        print(f"改善: {improvements}")
    if regressions:
        print(f"⚠️ 回归: {regressions}  ← 先看这些的 response 再决定要不要合并改动")


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="PickWise v2 eval harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run")
    pr.add_argument("--label", required=True, help="本次 run 的标签,如 baseline / after_rerank_fix")
    pr.add_argument("--only", help="只跑某个 category")
    pr.add_argument("--limit", type=int, help="最多跑 N 条(小批量试跑用)")
    pr.add_argument("--rpm", type=float, help=f"覆盖全局限速(默认 {MAX_RPM:.0f} RPM)")
    pr.add_argument("--no-judge", action="store_true", help="跳过 LLM judge,只跑规则层")

    pc = sub.add_parser("compare")
    pc.add_argument("run_a")
    pc.add_argument("run_b")

    args = p.parse_args()
    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        cmd_compare(args)
