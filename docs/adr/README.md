# Architecture Decision Records

Each ADR records one decision: the situation that forced it, the options on
the table, what was chosen, and what that choice costs. The **Not chosen, and
why** section is the point of the document — it is what stops the same
question being re-argued from scratch.

## Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-remove-the-fastmcp-layer.md) | Remove the FastMCP layer: tools bind to the agent as Python functions | Accepted | 2026-07-06 ¹ |
| [0002](0002-absorb-the-crs-pipeline-into-search-laptops.md) | Absorb the CRS pipeline into the `search_laptops` tool | Accepted | 2026-07-06 ¹ |
| [0003](0003-the-agent-replaces-dialogue-management.md) | The agent replaces dialogue management | Accepted | 2026-07-06 ¹ |
| [0004](0004-relevance-gate-threshold-0.53.md) | Relevance gate threshold of 0.53, calibrated per embedding model | Accepted | 2026-07-17 ¹ |
| [0005](0005-llm-as-judge-graded-against-tool-output.md) | LLM-as-judge, graded only against tool output | Accepted | 2026-07-08 ¹ |
| [0006](0006-pickscore-positioning.md) | Position PickScore as a buy indicator, not a ranker | Accepted — amended 2026-08-20 | 2026-08-14 |
| [0007](0007-brand-as-a-soft-preference.md) | Brand is a soft preference on the agent path, not a hard filter | Accepted — open question on penalty size | — ¹ ² |
| [0008](0008-price-fabrication.md) | Price fabrication — a prompt guard and a deterministic check are not alternatives | Accepted | 2026-08-17 |
| [0009](0009-a-status-column-with-two-kinds-of-hidden.md) | A `status` column with two kinds of hidden | Accepted — amended 2026-08-21 | 2026-08-17 |
| [0010](0010-benchmark-resolution-anchor-tokens-vs-threshold.md) | Resolve benchmarks by structure, not by confidence | Accepted — amended 2026-08-21, 2026-09-15 | 2026-08-17 |
| [0011](0011-normalize-by-percentile-rank-not-by-min-max.md) | Normalize by percentile rank, not by min-max | Implemented 2026-08-17, verified | 2026-08-17 |
| [0012](0012-review-linkage.md) | Review linkage: many-to-many with claim scope | Proposed | 2026-08-22 |
| [0013](0013-two-stage-matching.md) | Two-stage matching: family first, configuration second | Proposed | 2026-08-22 |
| [0014](0014-non-english-review-coverage.md) | Non-English review coverage: ASR declined | Accepted | 2026-08-22 |
| [0015](0015-one-definition-of-gpu-strength.md) | One definition of GPU strength: the reranker defers to PickScore | Accepted | 2026-09-14 |
| [0016](0016-what-an-unresolved-benchmark-means.md) | What an unresolved benchmark means | Accepted | 2026-09-15 |

¹ Written on 2026-10-11, after the fact. The date is when the decision was
made, taken from the conversation, code comment or eval run that records it.
Where the reasoning at the time was not written down, the ADR says so and
marks the reconstructed parts.
² When ADR-0007's behaviour was introduced is not recorded; it was in place
by 2026-08-15.

## By area

**Agent and conversation** — 0001, 0002, 0003

**Retrieval pipeline** (`app/rag/`) — 0002, 0004, 0007, 0015

**PickScore** — 0006, 0010, 0011, 0015, 0016

**Catalog data** — 0009 (laptop status), 0010 (benchmark resolution)

**Evaluation** — 0005, 0008

**YouTube reviews** — 0012, 0013, 0014

## Decisions that changed

- ADR-0009 supersedes ADR-0006's rule to exclude unpriced laptops when a
  budget is given — `status` now does that job.
- ADR-0010's confidence threshold was raised and later split into CPU (0.90)
  and GPU (0.85) halves; both amendments are inside the document.
- ADR-0008 is where ADR-0005's judge reaches its limit: it cannot catch a
  plausible fabricated price, so a deterministic check was added.
- ADR-0015 removed the reranker's GPU keyword bonus, which made the brand term
  the largest lever in the reranker — the open question recorded in ADR-0007.

## Writing a new ADR

Take the next number. Use this shape (the existing ADRs follow it):

```markdown
# ADR-NNNN — <the decision, in one line>

- **Status:** Proposed | Accepted | Implemented | Superseded by ADR-XXXX
- **Date:** YYYY-MM-DD
- **Related:** ADR-XXXX (why it is related)

## Context
What was true, what constrained the choice, what triggered it. Numbers
where they exist.

## Options considered
1. …
2. …

## Decision
What was chosen, and the one reason that decided it.

## Consequences
- What this buys
- What it costs
- Under what conditions this decision stops holding
- Known gaps left open

## Not chosen, and why
Each rejected option and the reasoning.
```

When a decision changes, do not rewrite history. Either append a dated
**Amendment** section to the existing ADR (as 0006, 0009, 0010 and 0011 do),
or write a new ADR and mark the old one `Superseded by ADR-XXXX`. Then update
the table above.
