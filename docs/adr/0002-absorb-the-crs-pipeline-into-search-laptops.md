# ADR-0002 — Absorb the CRS pipeline into the `search_laptops` tool

- **Status:** Accepted
- **Date:** 2026-07-06 (recorded 2026-10-11, after the fact)
- **Related:** ADR-0001 (no FastMCP layer), ADR-0003 (agent replaces dialogue
  management), ADR-0004 (gating threshold), ADR-0006 (PickScore positioning),
  ADR-0009 (laptop status)

## Context

In early July 2026 the backend had **two chat systems** answering the same
kind of question:

| | `POST /conversations/{id}/chat` (CRS) | `POST /agent/chat` (agent) |
|---|---|---|
| Shape | Fixed five-step pipeline | ReAct agent choosing tools |
| Retrieval | pgvector top-50 | pgvector, directly |
| Rerank / relax / gate | Yes | **No** |
| Intent detection | Separate Gemini call per follow-up | None |
| Review evidence, Apple pricing | **No** | Yes |

The frontend was a **single chat box**: users type anything into one input.
That made the split a real problem rather than a tidy separation:

- Sending everything to the CRS lost review evidence and Apple configuration
  pricing.
- Sending everything to the agent lost the precision logic the CRS had —
  reranking against budget and weight, stepwise constraint relaxation, and the
  relevance gate. The agent's `search_laptops` was a plain database search.

And underneath both, the same "embed → pgvector cosine → filter" step was
implemented more than once (`/laptops/hybrid-search`, CRS Module 1, the
agent's search).

The stated product goal decided the direction: a buying assistant that behaves
like a human salesperson — one that decides for itself whether to search,
ask, or answer — not a system that walks every message through a fixed
sequence.

## Options considered

1. **Keep both endpoints**, and route between them in the frontend.
2. **Wrap the whole CRS as one agent tool** (`get_recommendations`), including
   its own LLM reply.
3. **Expose each CRS stage as a separate tool** (retrieve, rerank, relax,
   gate) and let the agent chain them.
4. **Put the CRS stages inside `search_laptops`** as library code, delete the
   CRS chat endpoint, and make `/agent/chat` the only entry point.

## Decision

**Option 4.** The five modules stay in `app/rag/` as internal library code.
`search_laptops` (`app/agent/tools/search_laptops.py`) calls them in a fixed
order on every invocation:

```
retrieve_candidates (top-50 pgvector)
  → rerank            final_score = similarity × penalty + bonus
  → relax_and_retry   only if no candidate clears _MIN_VIABLE_SCORE
  → relevance_gate    top final_score vs RELEVANCE_THRESHOLD (0.53)
  → family dedup → cap at _MAX_RESULTS → PickScore on the survivors
```

`POST /conversations/{id}/chat` was removed. `app/rag/router.py` keeps only
conversation-thread CRUD and `GET /{id}/laptops`; all chat goes through
`/agent/chat` and `/agent/chat/stream`.

The deciding reason: **the agent decides *whether* to search; the pipeline
decides *how*.** Precision logic is not optional, so it must not be something
the agent can skip. Putting it inside the tool means every search the agent
makes gets reranking, relaxation and gating, whatever the model decides about
everything else.

## Consequences

**What this buys**

- One chat product, one entry point, one place where retrieval quality is
  decided.
- Every `search_laptops` call is logged to `pipeline_eval_logs` and
  `logs/eval/pipeline_trace.jsonl` by `log_pipeline_result()`, so the
  pipeline's behaviour stays observable even though an LLM now drives it.
- Filters placed inside the library cover every path at once. The active-only
  filter (ADR-0009) lives in `retrieve_candidates` rather than in the tool for
  exactly this reason: relaxation retries and `rag/evaluation.py` re-enter
  through the same function.

**The tool now has a contract the agent must honour**

A gated search returns no laptops, plus `confidence: "low"`, a `bottleneck`
and a ready-made clarifying `message`. The pipeline no longer writes the
reply, so turning that into a good question is the agent's job, enforced only
by the system prompt ("you MUST NOT simply say no laptops were found and
stop"). Relaxation likewise returns a `relaxation_notice` the agent is told to
mention. A prompt instruction is weaker than code; whether the agent actually
does this is checked by the `constraint_relaxation` eval category, not
guaranteed.

**What it costs**

- **Tokens.** Every result the tool returns is re-sent to the model on every
  later reasoning step. This is why `_MAX_RESULTS = 6` caps the list whatever
  `top_k` the model asks for, and why `_to_result_dict` strips the reranker's
  diagnostics (`final_score`, `penalty_reasons`, `bonus_reasons`) and sends
  only a top-3 PickScore factor summary.
- **Latency.** An agent turn is at least one model call before the search and
  one after, against a fixed pipeline's single reply call.

**Known gaps**

- **Retrieval is still not one implementation.**
  `POST /laptops/hybrid-search` keeps its own pgvector query and applies
  `brand` as a hard SQL filter — the opposite of the agent path (ADR-0007).
  *(Update 2026-10-22: the second copy, `app/recommendation/service.py` /
  `POST /recommendations/laptops`, was removed — nothing called it.)*
- `relax_and_retry` re-enters `retrieve_candidates` with the relaxed budget
  but not `weight_max`. On the semantic path weight is still applied by the
  reranker, so this only matters on the relational fallback, where weight is
  filtered in SQL.
- The CRS was designed with a dialogue "belief state" feeding constraints to
  the reranker. That component was never built; constraints now come from the
  tool's own arguments, which the model fills in each turn (ADR-0003).

## Not chosen, and why

**Keeping both endpoints (option 1).** With a single chat box, the frontend
would have to decide which system handles each message — which is intent
classification moved into the client. It also leaves two implementations of
the same retrieval to keep in step.

**Wrapping the whole CRS as one tool (option 2).** The CRS ended in its own
LLM reply. Calling it from an agent means two models writing one answer, and
the CRS's separate intent detection on top of the agent's own judgement —
two layers deciding the same thing.

**One tool per stage (option 3).** Not discussed at the time; the reasoning
is reconstructed. It would let the agent chain retrieve →
rerank → relax → gate itself, which also lets it skip the gate or relax
before reranking. The stages only make sense in one order, so the order
belongs in code. It would also multiply the payloads the model has to carry
between calls.
