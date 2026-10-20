# PickWise 2.1 Plan

Last updated: 2026-10-20

## 1. Positioning

**Core promise:** laptop recommendations that are available to buy in Malaysia, backed by real data, and able to explain why.

Why the positioning is changing: PickWise's real competitor is ChatGPT / Gemini. If its answers look the same as theirs, nobody has a reason to open another website. What sets PickWise apart:

- It only recommends models you can actually buy in Malaysia, and every price has a source. Nothing is made up.
- Comparisons use real benchmarks, not an AI's impression.
- Every recommendation can explain why, and can point to where each claim comes from.

Different user groups (students, people who don't understand specs, people deciding whether to upgrade, companies, gamers) are **different entry points** to the same engine. There is only one core promise.

---

## 2. Decisions made

| Topic | Decision |
|---|---|
| Reviews in PickScore | **No.** Review data is shown separately. PickScore uses only benchmarks and specs |
| Price source | There is no stable API that says which seller sells at what price, so marketplace prices are not shown. Show the official price + last-updated date + a note that "marketplaces may be cheaper", plus Shopee / Lazada search links by model |
| `matched_laptop_id` | **Drop it.** `review_laptop_link` becomes the single source of truth. Backfill first (3 matched reviews have no row in the link table) |
| Claims about other laptops in comparison videos | Attach them to the laptop being talked about. Approach: Review C flags them and does not attach yet; they are attached only after Review D's automated matching is in place |
| Review scheduling | Runs on Jack's own PC (Windows Task Scheduler), because transcripts can only be fetched locally (Webshare is not set up) |
| Laptops with no reviews | Be honest. This laptop has reviews → show them; it has none but its family does → show the family's, labelled "from other configurations in the same family"; neither → show "no reviews yet" |
| Review topic tags | Yes. When Gemma processes each chunk, the same call also tags a topic (thermals, build, display, battery, performance, etc.) |
| XAI (Explainable AI) | All four directions (see section 4) |
| Thumbs up / down feedback | In scope for 2.1 |
| Software compatibility | Software first, games later |
| Price drop alerts, accessory suggestions | Not doing these. The commented-out code has been removed |
| LLM switching | Not tied to one model. Both automatic fallback (primary fails → try the backup) and manual switching |
| Backup model | Google's own models for now |
| Where fallback applies | Agent, recommendation, review processor. **Not** the eval judge (scores must stay comparable) and **not** embeddings (vectors and the 0.53 gating threshold are tied to one embedding model) |
| Where the LLM setting lives | An admin page, so the model can be switched without a redeploy. Stored in the existing `app_settings` key/value table (which already holds `processor.model`), not a new table |
| How fast fallback happens | Within 1 minute. The primary gets a single attempt (no retries on the same model) and a 60 s total deadline per LLM call; a 429 switches immediately. Switching only happens before the answer starts streaming to the user, never mid-stream |

---

## 3. Order

| # | What | Why here |
|---|---|---|
| 1 | Connection pool fix + LLM timeout + fallback + admin switch | Fixes the production QueuePool exhaustion at its root (Gemini has no timeout, and transactions stay open across slow calls), and every later step that calls an LLM benefits from fallback |
| 2 | Review A: fix the foundation + drop `matched_laptop_id` | Everything after depends on it. Scaling up on dirty data only adds more dirty data |
| 3 | Review B: read by family + public endpoint + details page | The manual links already done (26 videos) become useful immediately |
| 4 | Price: official price + date + search links | Small, and XAI D needs the date |
| 5 | XAI A: show the PickScore breakdown in general mode too | Extends something that already exists; small change |
| 6 | Thumbs up / down | The earlier it ships the better, since feedback data takes time to build up |
| 7 | Review C: topic tags + flag claims about other laptops | Must happen before large-scale ingest, otherwise every chunk has to be re-run through Gemma later |
| 8 | Software compatibility | The centrepiece of the new positioning; the largest piece of work |
| 9 | XAI B, C: comparative explanations, constraint relaxation | Needs stable recommendation results first |
| 10 | Review D + E + scheduling | Automated matching, attaching the flagged comparison claims, raising coverage |
| 11 | XAI D: a source for every claim | Needs reviews, price dates and benchmarks all in place before there are sources to attach |
| F | Run an independent check after each stage | The thing that fixes a bug and the thing that checks the fix must be separate |

---

## 4. Details

### Connection pool fix + LLM timeout + fallback

**Background.** Production keeps hitting `QueuePool limit of size 20 overflow 20 reached`. A read-only investigation (2026-10-20) found no leaked sessions. The cause is database transactions held open while waiting on slow external calls (Gemini, embedding, YouTube), with no timeout on Gemini. Switching from port 6543 to 5432 did not help, which matches this: the connections are stuck in Python, not in Supabase. Production is now on 5432, so the existing QueuePool stays (no `NullPool` needed). Raising the pool size is not the fix.

**Connection pool fixes**
- End the database transaction before any slow call: read what is needed, commit / close, then call the LLM, embedding or YouTube. Known sites: `recommendation/service.py`, `search_laptops` (embed before the preference query, as `hybrid_search` already does), review ingest / `/retry-transcripts` / `/backfill-descriptions` / processor first chunk, the cold `ranges_cache` build, Google login avatar fetch
- Set `idle_in_transaction_session_timeout` on Supabase as a safety net, so a transaction left idle is closed by Postgres
- Run the `pg_stat_activity` query from the investigation before and after, to confirm `idle in transaction` rows are gone

**Prerequisite for fallback: timeouts.** Today `ChatGoogleGenerativeAI` runs with `timeout=None` and `max_retries=6`, and the embedder sets no request timeout. A stalled call never fails, it just waits, so a fallback would never be triggered. It is also the main cause of the production QueuePool exhaustion: a stuck call keeps its database connection pinned.
- Set an explicit timeout and fewer retries on every LLM and embedding call
- Done in Phase 1 (2026-10-21): `LLM_MAX_RETRIES=1` (one attempt on the same model; in Phase 2 the backup model is the retry), `LLM_TOTAL_DEADLINE_S=60` wall-clock per LLM call across attempts (`app/common/llm.py`; the httpx timeout is only per read), for streams until the first chunk reaches the user. Embedding stays at 15 s / 2 attempts

**Model setup**
- Build chat models through LangChain's `init_chat_model`, so provider + model is a setting (e.g. `"google_genai:gemma-4-31b-it"`) rather than a hard-coded class
- Wrap them with `.with_fallbacks([...])` so a 429, timeout or server error on the primary automatically tries the backup
- Backup: a Google model for now (which one is still to be decided)
- A 429 fails fast and goes straight to the backup (no backoff and retry on the primary first)
- Never switch models once streaming to the user has started: fallback only before the first token; after that, an error ends the stream as an error
- One fallback mechanism only: reuse or replace the processor's existing overload-only `_invoke_with_fallback` (`app/processor/engine.py`), not a second one beside it

**Scope**
- Fallback on: agent, recommendation chain, review processor
- No fallback: eval judge (must stay fixed so runs are comparable), embeddings (switching would require re-embedding the whole catalog and re-deriving the 0.53 gating threshold; the existing relational fallback covers embedding outages)

**Admin page**
- Choose the primary and backup model per use (agent / recommendation / review processor) without a redeploy
- Stored in the existing `app_settings` key/value table (it already holds `processor.model`), not a new table; values validated in code, with the env values as defaults when no row exists
- The model dropdown is filled from the Gemini API's `models.list` (`GET /v1beta/models`), filtered to models whose `supportedGenerationMethods` include `generateContent`. Cache the list rather than calling it on every page load
- Show `inputTokenLimit` / `outputTokenLimit` next to each model
- Warn when a configured model no longer appears in the list (retired)
- Known limits of `models.list`: it gives no rate limits, quota or pricing, and says nothing about tool-calling quality. Gemma entries have been reported to omit the `description` field, so the parser must not require it
- Manual switch overrides the primary; automatic fallback still applies underneath

**Observability**
- Record which model actually answered (primary or backup) in `agent_run_logs` and show it on the monitoring page

**Before trusting a backup model**
- Run the eval harness against it: tool calling must work, and the `PRICE CLAIMS` / `SCOPE ENFORCEMENT` prompt blocks were tuned for Gemma, so behaviour may differ

### Review pipeline

**A. Fix the foundation**
- Remove duplicate chunks (11 of 15 videos were processed twice); add an "already processed" guard to `/process/{id}`
- Partially processed reviews must be retryable (today, saving 3 of 40 chunks counts as done)
- `/rematch` must not override human decisions
- `DELETE /links/{id}` must clean up consistently
- Steps to drop `matched_laptop_id`:
  1. Backfill: for every row where `matched_laptop_id` is set but the link table has no row, add a link
  2. Change every reader and writer (ingest, `/rematch`, processor, admin endpoints, link deletion) to use only the link table
  3. Drop the column with an Alembic migration last

**B. Get links to the agent and to users (ADR-0012 phase 2)**
- Add `family_id` and `scope` to chunks (does this claim apply to the whole family, or one configuration?)
- Processor reads links
- `get_review_evidence` searches by family + config, applies a relevance cutoff, and deduplicates before returning
- Aggregator works per family
- Public endpoint, e.g. `GET /laptops/{id}/reviews`
- Frontend wires up the existing `review-sentiment.tsx` (the adapters currently hard-code an empty sentiment)
- Details page follows the "laptops with no reviews" rule above

**C. Comparison videos + topic tags**
- While processing a chunk, Gemma decides which laptop the claim is about; claims about other laptops are flagged and not attached
- The same call adds a topic tag (thermals, build, display, battery, performance, other)
- Uses: group reviews by topic on the details page; later, the Build / Thermals / Display axes on the compare page radar chart (shown separately, not part of PickScore)

**D. Automated matching (ADR-0013)**
- Match the family first, then the configuration
- Use `tie_width` to decide: auto-accept / human queue / discard
- Once in place, attach the claims flagged in C to the laptop they are about

**E. Raise coverage**
- Ingest the most frequently recommended families first
- Schedule on Jack's PC with Task Scheduler

**F. Independent checks**
- Audit script measuring coverage, duplicate rate, and the share of claims attached to the wrong laptop

### Price
- Details page shows the official price + last-updated date (from the existing price history)
- Note: "Official price. Marketplaces may be cheaper"
- Shopee / Lazada search links by model

### XAI
- **A. Extend what exists:** show the PickScore breakdown in general mode too (users who haven't done the questionnaire)
- **B. Comparative explanations:** why recommend A over B, e.g. "RM500 more, but the GPU is 40% faster"
- **C. Constraint relaxation:** when weight or budget is relaxed, tell the user, and write it back to dialogue state so the next turn can confirm it
- **D. Sources:** attach a source to each claim, e.g. review timestamp, price date, benchmark name

### Thumbs up / down
- One feedback table + endpoint
- 👍👎 on chat replies
- Start with the simplest version; no micro-surveys, no "unlock Pro mode"
- Purpose: real user data on recommendation quality, not only the offline eval

### Software compatibility
- Minimum / recommended requirements per software (CPU, GPU, RAM, Windows-only or not, dedicated GPU or not)
- Course → software mapping
- Add more models under RM4000 (student budgets; this band is thin today)
- Not decided yet: hard constraint, PickScore factor, or both (note: this is about software requirements, separate from the "reviews not in PickScore" decision)

---

## 5. To decide later

- Which Google model is the backup (picked on the admin page, after it passes the eval harness)
- How many courses, and which ones, in the first software compatibility batch (suggested: 5–10)
- Whether software requirements are a hard constraint, a PickScore factor, or both
- Whether the compare page radar chart actually switches to 6 axes (revisit once Review C's topic tags have enough coverage)
- Whether the Shopee / Lazada affiliate programs offer an API for looking up prices by model (needs signing up to check)
- Proving PickWise is more trustworthy than ChatGPT: run the same questions through ChatGPT / Gemini and PickWise using the existing eval harness, and compare the rate of made-up prices and non-existent models (can be done at any time)

---

## 6. Background: review pipeline state (measured 2026-10-19)

- Videos: 93 (matched 21, pending 47, rejected 20, irrelevant 5)
- Chunks: 496 rows, 205 of them duplicates, so 291 unique
- Laptops with reviews: 7 of 333 active (2.1%)
- Active laptops in a family that has chunks: 21, but the tool does not search by family, so only those 7 get evidence
- Link table: 8 auto, 13 human with a config, 47 human family-only (on 26 videos)
- Summaries: 10 rows, 3 of them empty, and not visible to users
- Last chunk written: 2026-07-18; last ingest: 2026-08-25
- ADR-0012: partly implemented; the link work does not reach the agent yet
- ADR-0013: automated part not started; `tie_width` is always null
- Every stage is manual; nothing is scheduled
