# ADR-0003 — The agent replaces dialogue management

- **Status:** Accepted — agent constructor later migrated (see Note)
- **Date:** 2026-07-06 (recorded 2026-10-11, after the fact)
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0006
  (PickScore positioning), ADR-0009 (laptop status)

## Context

The CRS chat flow managed the conversation itself. On every follow-up message
it made a **separate Gemini call to classify intent** — `related` (a question
about laptops already shown) or `new_search` — and used the answer to decide
whether to replace the conversation's shortlist (`conversation_laptops`) or
reuse it.

Once the CRS was absorbed into the agent's search tool (ADR-0002), that
classifier became a second decision-maker sitting in front of a component
whose whole job is to decide. A ReAct agent already looks at the message and
the history and chooses: search again, call another tool, ask a question, or
answer from what it has. Keeping the classifier would mean two models deciding
"is this a new search?" for every message, with nothing to reconcile them when
they disagree.

There was also a question of **where conversation state lives.** The PRD
mentioned LangGraph's state management for long-running threads. But the
conversation was already in PostgreSQL — the `conversations`, `messages` and
`conversation_laptops` tables — and the frontend reads those tables to render
threads.

## Options considered

1. **Keep the intent classifier** in front of the agent, and pass its verdict
   in.
2. **Move intent handling inside `search_laptops`**, so the tool decides
   whether a call is a refinement or a new search.
3. **Let the agent decide**, with no classifier, and store state with a
   LangGraph checkpointer.
4. **Let the agent decide**, with no classifier, and rebuild its context from
   the existing PostgreSQL tables on every turn.

## Decision

**Option 4.** There is no intent-detection call. On each turn
`_build_agent_input()` in `app/agent/graph.py` rebuilds the agent's context
from the database:

```
SystemMessage  _SYSTEM_PROMPT
SystemMessage  user's saved preference profile   (if any)
SystemMessage  "Current shortlist" from conversation_laptops (active rows only)
last _HISTORY_WINDOW (= 8) user/assistant messages from `messages`
HumanMessage   the new message
```

The system prompt tells the agent: for follow-up questions about laptops
already shown, answer from that context instead of calling `search_laptops`
again. The agent's choice of tool *is* the intent decision.

After the turn, `app/agent/router.py` persists the user and assistant
messages, and **replaces the `conversation_laptops` pool only when
`search_laptops` returned `confidence: "high"`** — a gated search does not wipe
the shortlist the user was looking at.

No LangGraph checkpointer is used. PostgreSQL is the single source of truth
for conversation state.

The deciding reason: the decision "search again or answer from what we have"
needs the full conversation in view, and the agent is the only component that
has it. A classifier in front of it can only be a weaker copy of the same
judgement.

## Consequences

**What this buys**

- One fewer model call on every follow-up turn.
- One place where the decision is made, so one place to look when it is wrong.
- State the frontend already reads is the state the agent sees. Nothing to
  keep in sync between a checkpointer and the `messages` table.
- Deactivating a laptop mid-conversation takes effect on the next turn,
  because the pool is filtered on read (`_pool_block` skips non-active rows —
  ADR-0009). With state frozen in a checkpointer, a retired laptop would stay
  in the agent's memory.

**What it costs**

- **The decision is a prompt behaviour, not code.** Nothing guarantees the
  agent re-searches when it should or answers from the pool when it can. It
  is measured by the `multi_turn` eval category, and that measurement is
  noisy: across three runs a single run moved by about one item in six
  (~17 percentage points), with `multi_zh_001` failing persistently
  (ADR-0008).
- **History is expensive.** The window is re-sent on every internal ReAct
  step, which multiplies against Gemini's free-tier tokens-per-minute limit.
  It was cut from 12 messages to 8 for that reason, and the agent's LLM calls
  are paced to one every five seconds (`_AGENT_RATE_LIMITER`).

**What the agent can and cannot answer from memory**

Only user and assistant *text* is persisted. Tool outputs are not, so on the
next turn the agent's knowledge of earlier results is whatever its own reply
said, plus the shortlist block — which carries id, name, price, CPU, GPU, RAM
and weight only. A follow-up about battery, storage or PickScore on a laptop
already shown cannot be grounded from the pool; the agent has to search again
or rely on its earlier reply.

**When this stops holding**

- If the model behind the agent gets weaker at tool choice, the missing
  classifier is felt directly. The decision depends on model quality in a way
  a fixed classifier would not.
- If conversations get long enough that 8 messages lose constraints the user
  stated early on, the agent will forget them. A structured belief state —
  constraints stored as data, not re-read from text — would then be worth
  building.

## Note — implementation migration

The agent was originally built with LangGraph's
`langgraph.prebuilt.create_react_agent`. It now uses
`langchain.agents.create_agent`, after the former was deprecated. The decision
here — no separate intent call, the agent chooses, state rebuilt from
PostgreSQL — did not change with it.

## Not chosen, and why

**Keeping the classifier (option 1).** Two decision-makers on the same
question, one with less context than the other. It adds a model call per turn
and a disagreement case with no owner.

**Intent handling inside the tool (option 2).** The tool only sees its own
arguments. It cannot know what the user was shown two turns ago unless the
conversation is passed into it, at which point it is re-implementing the
agent. It would also only run when the agent had already decided to search —
the case where the question is already answered.

**A LangGraph checkpointer (option 3).** It would store a second copy of the
conversation next to the `messages` table the frontend reads, and the two can
drift. It would also hold the shortlist as it was when the turn ran, so a
laptop retired later would remain recommendable from memory.
