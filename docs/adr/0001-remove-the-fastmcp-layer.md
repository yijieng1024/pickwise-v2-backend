# ADR-0001 — Remove the FastMCP layer: tools bind to the agent as Python functions

- **Status:** Accepted
- **Date:** 2026-07-06 (recorded 2026-10-11, after the fact)
- **Related:** ADR-0002 (CRS absorbed into the search tool), ADR-0003 (agent
  replaces dialogue management)

## Context

`Technical_Spec.md` planned a **FastMCP Server** as the "agent tool layer": an
MCP server exposing three tools — `pgvector_laptop_search`,
`calculate_custom_apple_price` and `search_malaysian_market_price` — for a
LangGraph agent to call over the Model Context Protocol.

By early July 2026 three things were true at once:

- **The server had no consumer.** The same spec stated that the two
  recommendation pipelines then running (the one-shot recommendation and the
  CRS chat) were "sequential Python modules — no LangGraph required". The MCP
  layer was being built for an agent that, on paper, did not exist.
- **The agent that did exist already called its tools directly.**
  `app/agent/` had a ReAct agent whose `search_laptops`,
  `calculate_custom_apple_price` and `get_review_evidence` tools queried
  PostgreSQL in-process. No MCP anywhere on that path.
- **`pgvector_laptop_search` would have been a third copy** of "embed the
  query → pgvector cosine search → filter", next to `POST
  /laptops/hybrid-search` and CRS Module 1. Same logic, a new wrapper.

The product direction was settled in the same discussion: PickWise is a
conversational buying agent that should behave like a human salesperson. MCP
is not part of what makes that work. It is a packaging choice for tools.

## Options considered

1. **Build the FastMCP server as specified**, and have the agent call tools
   through it.
2. **Bind tools directly** as Python functions on the agent (LangChain
   `@tool`), in the same process as the API.
3. **Build both**: direct binding for the agent now, an MCP server alongside
   for possible outside clients.

## Decision

**Option 2.** Tools are plain Python functions decorated with LangChain's
`tool`, collected in `app/agent/tools/__init__.py` as `ALL_TOOLS`, and passed
straight to the agent (`create_agent(llm, _agent_tools(user_id))` in
`app/agent/graph.py`). Every tool opens its own database session and queries
PostgreSQL directly. No HTTP hop, no protocol layer.

The deciding reason: MCP solves a **process boundary** — tools owned by one
team or service, used by clients somewhere else. PickWise has one client of
its tools, the agent, and it lives in the same process as the database code.
There was no boundary to cross.

## Consequences

**What this buys**

- One less moving part to deploy on Render's free tier: no second server, no
  transport, no serialization of tool arguments and results across a process.
- **Per-request tool binding is trivial.** `make_search_laptops(user_id)`
  builds a `search_laptops` bound to the authenticated user so PickScore comes
  out personalized, while `user_id` is never an argument the LLM can see or
  fabricate. Across an MCP boundary the same guarantee would need the caller's
  identity propagated through the protocol.
- **The eval harness measures production exactly.** `eval/run_eval.py`
  imports `ALL_TOOLS` and `build_agent_llm()` and runs the agent in-process,
  so an evaluation can never drift from what users get because of a
  transport difference.
- Tools are easy to test: `search_laptops` looks up retrieval and scoring as
  module-level names at call time, so the Tier 5 tests stub them without
  standing up a server.

**What it gives up**

- The tools cannot be reused by another MCP client (Claude Desktop, another
  agent, another team) without extra work.
- "Uses MCP" is not something the project can claim.

**When this stops holding**

- A **second consumer** appears that does not live in this process — another
  agent, an external partner, a desktop client. At that point the tools have
  a real boundary to cross, and extracting them behind an MCP server is the
  right move.
- The tool set grows large enough that it should be owned and versioned
  separately from the agent.

The order matters: bind directly first, extract to MCP once the tool design is
stable and there is someone on the other side. Building the server first
would have frozen tool signatures that changed many times afterwards —
`search_laptops` alone has since gained `weight_max`, `purpose` validation, a
`score_mode` field and the `pick_score_unavailable` signal.

## Not chosen, and why

**Building the FastMCP server as specified (option 1).** It would have added
a transport layer between an agent and functions sitting in the same process,
for a tool set with exactly one caller. The spec also wrapped retrieval as
`pgvector_laptop_search` — the third implementation of the same search, which
is the duplication this decision set out to remove.

**Building both (option 3).** An MCP server nobody calls is code that has to
be kept in sync with every tool change and is tested by nothing. It is cheap
to add later and expensive to carry now.
