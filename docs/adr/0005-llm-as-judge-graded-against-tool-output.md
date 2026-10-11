# ADR-0005 — LLM-as-judge, graded only against tool output

- **Status:** Accepted
- **Date:** 2026-07-08 (first baseline run; recorded 2026-10-11, after the fact)
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0004 (gating
  threshold), ADR-0008 (price fabrication — the limit of this decision)

## Context

Once the agent became the only chat path (ADR-0002), the thing to test was no
longer a pipeline with a ranked list as output. It was a free-text reply, in
English, Chinese or Manglish, produced by a model that chooses its own tools.
Two replies can both be correct and share no sentence. Exact-match assertions
do not work on that.

Some properties *are* checkable in code — which tools were called, whether a
forbidden tool was called, whether the cheapest recommended price is within
budget. Others are not: does the reply address what the user asked, is the
budget reasoning consistent, does it refuse when it should, does it present a
gated search as a question rather than a dead end.

A second problem shaped the design. **The catalog is newer than any model's
training data.** It lists M5 MacBooks, RTX 50-series laptops and Core Ultra
200-series parts. A judge allowed to use its own product knowledge would mark
real, current products as invented, and would accept prices that merely sound
plausible.

## Options considered

1. **Human review only** — read every reply after each change.
2. **Deterministic checks only** — tool calls, budgets, required phrases.
3. **LLM judge using its own knowledge**, grading whether the reply is
   accurate and helpful.
4. **Deterministic checks plus an LLM judge restricted to the tool outputs**,
   grading against a per-query rubric.

## Decision

**Option 4.** `eval/run_eval.py` runs each query in `eval/queries.yaml`
through the production agent in-process (same `build_agent_llm()`, same system
prompt, same tools) and grades it in two layers.

**Layer 1 — rule checks, in code** (`rule_checks()`):

| Check | What it asserts |
|---|---|
| `non_empty_response` | The agent said something |
| `required_tools_called` | Every tool in `expect.tools` was called |
| `forbidden_tools_not_called` | No tool in `expect.forbid_tools` was called |
| `primary_rec_within_budget` | The **cheapest** quoted price is within `budget_max × 1.05` (skipped for `constraint_relaxation`, which legitimately discusses over-budget figures) |
| `rm_figures_grounded` | Every RM figure appears in tool output (added later — ADR-0008) |

**Layer 2 — the judge** (`JUDGE_PROMPT`). The judge receives the user query,
the tools called, the raw tool outputs, the reply and the rubric, and returns
`{"pass": …, "reason": …}`. The prompt forbids it from using its own beliefs
about whether a product exists, whether a price is realistic or whether a chip
has been released. Factual claims are checked **only** against the tool
outputs; anything with no basis there counts as fabrication. Beyond that it
grades behaviour against the rubric.

A deterministic failure stands even if the judge is unavailable. A judge
failure never becomes a pass.

The deciding reason: the judge's job is to read a reply the way a reviewer
would, and the tool outputs are the only ground truth the system has. Letting
the judge use its own knowledge would test the reply against data older than
the catalog.

## Consequences

**Operating constraints that came with it**

- **Same model as the agent.** `JUDGE_MODEL = "gemma-4-31b-it"`, the agent's
  model, sharing one free-tier quota pool. (The module docstring's v3 note
  still says `gemini-2.5-flash`; the constant is what runs.)
- **One global rate limiter, 7 RPM by default**, shared by every agent and
  judge call. The binding limit is not requests but the 16,000
  input-tokens-per-minute cap: a judge prompt can carry ~27k characters of
  tool output, so two judge calls in one minute can 429. A full run takes
  20–30 minutes.
- **Truncation per output, never of the joined blob.** Each tool output is cut
  at 4,500 characters and the total at 27,000. At 3,000 per output the last
  results' PickScores were cut off, and the judge flagged correctly cited
  scores as fabricated. Head-truncating the joined text would hide later tool
  calls entirely and produce the same false fabrication verdicts.
- **Unscored is its own status.** After three failed attempts a judge verdict
  is recorded as `unscored`, not pass or fail. Unscored and errored entries
  are excluded from headline totals, and a run containing any must not be used
  for `compare`. This was added (v4) after the 2026-07-18 baseline counted
  three judge 429s as passes.

**What it found**

The harness caught a real scope-enforcement bug — the agent producing content
for requests outside its scope — fixed with the `SCOPE ENFORCEMENT`
system-prompt block, which now states that acknowledging a task is out of
scope and then doing it anyway is a violation.

**What it cannot see**

- **A plausible fabrication.** The judge is the same model as the agent, with
  the same priors. Told not to use its own knowledge, it has no way to know a
  confident price is wrong; given its knowledge, it would share the agent's
  belief. ADR-0008 found the agent inventing Malaysian price ranges and
  answered it with a deterministic check, because no rubric could.
- **Facts the tool never returns.** `_to_result_dict` emits a fixed set of
  fields (no ports, display panel, or keyboard). A rubric that asks about those
  cannot be graded against the tool output, so the judge is grading the
  agent's training knowledge after all.
- **Personalized scoring.** The harness runs the module-level `search_laptops`
  with no user, so `user_pref` is always `None` and personalized PickScore is
  never exercised.
- **The gate threshold.** The `relevance_gating` category tests the scope
  prompt via `forbid_tools`, not ADR-0004's 0.53.

**How much a single run can be trusted**

Across three runs of `multi_turn`, four of six items passed every time,
`multi_zh_001` failed every time, and `multi_zh_003` alternated. One run can
move by about one item in six — roughly 17 percentage points per category.
A single pass→fail flip is noise until it reproduces.

**Scope today**

42 queries in seven categories, six each: `clear_intent`, `vague_intent`,
`constraint_relaxation`, `relevance_gating`, `tool_routing`, `multi_turn`,
`decision_support`. The first five (30 queries) were the original set;
`multi_turn` and `decision_support` were added with multi-turn support.

**When this stops holding**

- If the agent moves to a different model, the "same model, shared quota"
  trade-off changes: a judge from another family no longer shares the agent's
  priors, but still has its own stale knowledge, so the tool-output-only rule
  stays.
- If the quota is paid for, the rate limiter and the truncation limits should
  be revisited — both exist because of the free tier.

## Not chosen, and why

**Human review only (option 1).** Fine for a first look, but not repeatable:
a regression in one category after a prompt edit has to be caught across 42
queries, in three languages, on every change.

**Deterministic checks only (option 2).** They catch tool routing and budget
violations and miss everything about whether the reply is any good. Most
categories — vague intent, relaxation, decision support — are behavioural.

**A judge using its own knowledge (option 3).** The catalog is newer than the
model. It would fail correct replies about products it has never heard of and
pass wrong ones that match its priors — which is exactly the error the agent
makes.

**NDCG@10 against labelled rankings.** `rag/evaluation.py` implements it, but
its ground-truth labels are placeholders, and it measures the ranking from
retrieval, not the reply the user reads. It belongs to ADR-0004's calibration
problem, not to agent evaluation.
