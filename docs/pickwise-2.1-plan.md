# PickWise 2.1 Plan

Last updated: 2026-10-21

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
| Price source | There is no stable API that says which seller sells at what price, so marketplace prices are not shown. Show the official price + "Last Checked" date + a note that "marketplaces may be cheaper", plus Shopee / Lazada search links by model |
| `matched_laptop_id` | **Drop it.** `review_laptop_link` becomes the single source of truth. Backfill first (3 matched reviews have no row in the link table) |
| Claims about other laptops in comparison videos | Attach them to the laptop being talked about. Approach: Review C flags them and does not attach yet; they are attached only after Review D's automated matching is in place |
| Review scheduling | Hybrid. Transcript fetching runs on Jack's own PC (Windows Task Scheduler), because YouTube blocks datacenter IPs and the Webshare residential proxy is not set up. Everything else (discovery, matching, processing into chunks) runs on the server |
| Review processor backups | Two flash-lite models added as extra backups for review processing (via the admin LLM settings page), because during the 2026-10-22 run the primary was overloaded for about half the calls and both primary and backup failed 7 times |
| Laptops with no reviews | Be honest. This laptop has reviews → show them; it has none but its family does → show the family's, labelled "from other configurations in the same family"; neither → show "no reviews yet" |
| Review topic tags | Yes. When Gemma processes each chunk, the same call also tags a topic (thermals, build, display, battery, performance, etc.) |
| XAI (Explainable AI) | All four directions (see section 4) |
| Thumbs up / down feedback | In scope for 2.1 |
| Software compatibility | Software first, games later |
| Price drop alerts, accessory suggestions | Not doing these. The commented-out code has been removed |
| LLM switching | Not tied to one model. Both automatic fallback (primary fails → try the backup) and manual switching |
| Backup model | Google's own models for now. Each use (agent, recommendation, review processor, catalog processor) has an **ordered list** of backups, tried in turn. The agent and recommendation are capped at 2 backups each (a user is waiting on both; worst case ~3 min if every model hangs); review processor and catalog processor have no cap. Catalog processor defaults restore its pre-Phase-2 chain: gemma-4-31b → gemma-4-26b → gemini-3.5-flash-lite → gemini-3.1-flash-lite. Defaults were picked by Claude Code and are evaluated **after** deploy |
| Model construction | `build_chat_model` in `app/common/llm.py`, not `init_chat_model`: the stock class from `init_chat_model` would drop Phase 1's 60 s deadline and the error classification fallback relies on. Same `"provider:model"` format; only `google_genai` supported today |
| Where fallback applies | Agent, recommendation, review processor. **Not** the eval judge (scores must stay comparable) and **not** embeddings (vectors and the 0.53 gating threshold are tied to one embedding model) |
| Where the LLM setting lives | An admin page, so the model can be switched without a redeploy. Stored in the existing `app_settings` key/value table (which already holds `processor.model`), not a new table |
| Quota | Google's Gemini API quota is per model, so falling back to another model on a 429 is useful |
| How fast fallback happens | Within 1 minute. The primary gets a single attempt (no retries on the same model) and a 60 s total deadline per LLM call; a 429 switches immediately. Switching only happens before the answer starts streaming to the user, never mid-stream |
| Scrapers | Replace every manufacturer laptop scraper with Scrapling. YouTube transcripts and SerpApi stay as they are |
| Scheduled scraping | Runs on GitHub Actions, once a week, after a block test confirms manufacturer sites don't block GitHub's IPs |
| Writing scraped data | Processed results are written straight into the catalog (no approval queue) |
| New laptops | Go live automatically, but only if they pass every go-live condition (see section 4); otherwise they stay inactive |
| After each scheduled run | Embeddings for new laptops and PickScore `generate-all` re-run automatically |
| Notifications | Build a PickWise notification feature: email (Brevo) + in-app notifications. Sent to Jack (admin) only for now; the schema should allow user notifications later |
| Large price changes | Still written straight to the catalog, but trigger a notification (e.g. a change of more than 20%) |
| Date shown with the price | "Last Checked": the last time the scraper confirmed the price, even if it didn't change |
| Access to Pico | Open. No login or Needs Wizard required. Not logged in → temporary chat; logged in without the wizard → Pico suggests completing it for better results |
| Temporary chats | Claimed automatically into the user's history when they register **or** log in from the same browser. Unclaimed ones are deleted after 3 days |
| Guest usage limit | 3 messages per day per guest ID (not per IP). Only messages the user sends count, including clicked suggested questions; Pico's own opening turns (details page, compare page, after the wizard) don't count |
| Pico's opening on a details page | A short intro to the laptop: which use cases it suits and why. Not personalised, so it can be cached per laptop |
| Nudge to register / complete the wizard | Pico says it (not a fixed UI card) |
| How the system picks Pico's behaviour | No extra LLM call to classify intent. Uses (1) the entry point the conversation started from, (2) known system state (logged in, wizard done, relaxation happened, gated result, etc.), (3) instructions attached to tool results |
| Suggested questions | Generated by the LLM, shown under the input box after **every** Pico answer; clicking one sends it straight to Pico |
| Recommendation after the wizard | Done by the agent, so the user can keep chatting |
| Use-case ranking pages | "View all" on each home-page use-case row opens a list page for that use case (top 20 first, with the option to see more), with a budget filter, the PickScore breakdown per laptop, and an "Ask Pico" button per laptop. Server-rendered with proper titles/descriptions for SEO. Honest error/empty states, no mock laptops |

---

## 3. Order

| # | What | Why here |
|---|---|---|
| 1 | Connection pool fix + LLM timeout + fallback + admin switch | Fixes the production QueuePool exhaustion at its root (Gemini has no timeout, and transactions stay open across slow calls), and every later step that calls an LLM benefits from fallback |
| 2 | Review A: fix the foundation + drop `matched_laptop_id` | Everything after depends on it. Scaling up on dirty data only adds more dirty data |
| 3 | Review B: read by family + public endpoint + details page | The manual links already done (26 videos) become useful immediately |
| 4 | Price: official price + date + search links | Small, and XAI D needs the date |
| 5 | XAI A: show the PickScore breakdown in general mode too | Extends something that already exists; small change |
| 6 | Entry-aware Pico + open access + temporary chat | The details and compare pages already exist; this makes them useful, and opens Pico to visitors without an account. Before feedback, so the feedback reflects the real entry points |
| 7 | Use-case ranking pages | Needs the PickScore breakdown (step 5) and the "Ask Pico" entry (step 6), so it is built once, complete, right after them |
| 8 | Thumbs up / down | The earlier it ships the better, since feedback data takes time to build up |
| 9 | Review C: topic tags + flag claims about other laptops | Must happen before large-scale ingest, otherwise every chunk has to be re-run through Gemma later |
| 10 | Manufacturer scrapers → Scrapling | Software compatibility needs more models under RM4000, which means scraping new laptops; switch the scrapers first so that work uses the new ones |
| 11 | Scheduled scraping + notifications | Keeps the catalog current. After Scrapling, so the old scrapers aren't scheduled and then replaced |
| 12 | Software compatibility | The centrepiece of the new positioning; the largest piece of work |
| 13 | XAI B, C: comparative explanations, constraint relaxation | Needs stable recommendation results first |
| 14 | Review D + E + scheduling | Automated matching, attaching the flagged comparison claims, raising coverage |
| 15 | XAI D: a source for every claim | Needs reviews, price dates and benchmarks all in place before there are sources to attach |
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
  - In B, `scope` is set by rule, not by Gemma: chunks from a config-level link get `config`; chunks from a family-only link get `family`. Gemma-assigned scope moves to Review C, so every chunk is re-run through Gemma once (scope + topic + sentiment + which laptop, together) instead of twice
- Processor reads links. A review linked to exactly one family (family-only) becomes processable; reviews linked to several families (comparison videos) wait for Review C
- Adding a human link moves the review from pending to matched, the same as manual match. Found in the B0 survey (2026-10-22): 17 reviews with a human family link were stuck in pending because adding a link didn't change the status, so the processor never picked them up. A one-off script moves them (dry run first)
- Family-scope chunks from family-only reviews are shown on the public details page and to the agent, each labelled "configuration not stated". Accepted trade-off until Review C: a performance claim (frame rate, temperature, battery hours) from an untested configuration can appear on sibling pages, with that label. The agent must say so when it uses them
- Summaries on the public endpoint are computed from chunks on each request, not stored, so they can't go stale and the manual aggregate step goes away. `laptop_review_summary` stays for admin use for now
- Agent evidence (`get_review_evidence`), per question: first this laptop's own chunks plus its family's family-scope chunks; if none of those pass the 0.44 relevance cutoff, fall back to other configurations in the same family, labelled "tested on <config>". The two tiers are never mixed. Other families are never used. Each line carries its label ("this configuration" / "configuration not stated" / "tested on <config>"), and the result tells the agent to say so whenever a line isn't this exact configuration. The cutoff (`REVIEW_EVIDENCE_MAX_DISTANCE`, 0.44) guards against filler; it isn't a precision filter, and should be re-derived after Review C or once there are more real agent queries
- `get_review_evidence` searches by family + config, applies a relevance cutoff, and deduplicates before returning
- Aggregator works per family
- Public endpoint, e.g. `GET /laptops/{id}/reviews`
- Frontend wires up the existing `review-sentiment.tsx` (the adapters currently hard-code an empty sentiment)
- Details page follows the "laptops with no reviews" rule above
- Decode HTML entities in stored video titles (e.g. `&amp;` → `&`): the YouTube API returns titles HTML-escaped and they are stored as-is. Fix before titles are shown to users; it also adds a stray "amp" token to fuzzy matching

**C. Comparison videos + topic tags**
- While processing a chunk, Gemma decides which laptop the claim is about; claims about other laptops are flagged and not attached
  - Known case to fix here: `TEJdZf31x6s` ("MacBook Neo vs Air vs Pro") is a comparison video linked only to the MacBook Neo family, so all 28 of its chunks sit on the Neo, including claims about the Air and Pro. The link rule can't catch it because the link names one family; re-running every chunk through the which-laptop check will
- The same call assigns `scope` (family vs config), replacing the rule-based scope set in Review B. Every existing chunk is re-run once through the new call
- Compare against Laya during the accuracy check: run the same hand-labelled sample through Gemma and through Laya's fine-tuned `laya-typed-decisions` checkpoint (https://github.com/NandhaKishorM/laya, Apache 2.0, self-hosted, `pip install laya`, multilingual routing for Chinese titles/transcripts), for sentiment, topic and scope. Compare accuracy and speed. If Laya is clearly better, or about as accurate and much faster, consider using it for the classification part (Gemma still does the paraphrase). Note: Laya's confidence scores need calibrating on our own data, and it cannot decide which laptop a claim is about (open-ended catalog), so that stays with Gemma
- The same call adds a topic tag (thermals, build, display, battery, performance, other)
- Improve the strength / weakness / neutral label in the same call. Seen on 2026-10-22: a chunk whose main point was a weakness ("8GB will struggle with longevity") was labelled STRENGTH. Check accuracy on a sample of chunks before and after the change
- Uses: group reviews by topic on the details page; later, the Build / Thermals / Display axes on the compare page radar chart (shown separately, not part of PickScore)

**D. Automated matching (ADR-0013)**
- Match the family first, then the configuration
- Use `tie_width` to decide: auto-accept / human queue / discard
- Once in place, attach the claims flagged in C to the laptop they are about

**E. Raise coverage**
- Ingest the most frequently recommended families first
- Transcript fetching scheduled on Jack's PC with Task Scheduler; the rest runs on the server

**F. Independent checks**
- Audit script measuring coverage, duplicate rate, and the share of claims attached to the wrong laptop

### Entry-aware Pico + open access + temporary chat

**How Pico's behaviour is chosen, without asking the user and without an extra LLM call**
1. Entry point: the frontend sends where the conversation started (and which laptop(s) were on screen)
2. System state the backend already knows: logged in or not, wizard completed or not, relaxation happened, gated low-confidence result, laptops already recommended in this thread
3. Instructions attached to tool results: guidance on how to present a tool's output travels back with that tool's result, so the system prompt stays short and only the relevant guidance is seen

A separate LLM call to classify intent is deliberately not used (extra latency and quota on a rate-limited model, and a wrong guess derails the whole answer; v1 had this problem). Keyword / embedding guessing is not used either.

**Entry points**

| Entry | Who speaks first | What Pico does |
|---|---|---|
| Home omnibar | User | Answers the question directly |
| "Ask Pico" on a laptop details page | Pico | A short intro to this laptop: which use cases it suits and why. The use-case fit comes from the laptop's PickScore use-case scores and specs, not the model's own opinion. Same for every user, so it is cached per laptop and regenerated when the laptop's data, price or PickScore changes |
| Compare page | Pico | Gives a more detailed comparison of the laptops on screen |
| Needs Wizard completed | Pico | Recommends laptops straight away, no prompt needed. Done by the agent so the user can keep chatting |

When Pico speaks first, the backend builds an internal instruction from the entry context. It is not shown as a user message.

**Suggested questions**
- Generated by the LLM and shown under the input box after every Pico answer
- Clicking one sends it straight to Pico, which starts answering

**Access and nudges**
- Open to everyone. Not logged in → temporary chat. Logged in without the wizard → Pico suggests completing it for better results. Not logged in → after answering, Pico suggests registering and completing the wizard for the full experience
- The nudge comes from Pico. Its frequency is controlled by system state (e.g. only once per conversation) rather than left to the model, so it doesn't repeat every turn

**Temporary chat**
- A random guest ID in an HttpOnly cookie (not localStorage, so page scripts can't read it and nobody can claim someone else's chats)
- On register **or** login from the same browser, the guest's conversations move to the user's history automatically
- Unclaimed temporary chats are deleted after 3 days
- Guest limit: 3 messages per day per guest ID, to protect the shared Gemma quota. Not counted per IP (people on the same Wi-Fi would share one limit). Only user-sent messages count, including clicked suggested questions; Pico's opening turns don't. Known gap: clearing cookies gives a new guest ID and resets the limit

### Use-case ranking pages

- Entry: "View all" on each use-case row on the home page
- One page per use case (e.g. `/rankings/gaming`), showing the top 20 first; a "Load more" button appends the next 20 below (the server-rendered first 20 are what search engines index). Uses the existing ranking logic (family dedup: one config per product line; proxy demotion where it already applies)
- Budget filter, so users can see the top 20 within their budget (the unfiltered gaming list is led by RM20,000+ machines)
- Per laptop: name, official price + "Last Checked", PickScore, key specs, the PickScore breakdown (same as XAI A), and "Ask Pico" (opens the details-page entry from the entry-aware Pico step). Clicking the laptop opens its details page
- Server-rendered, with a page title and description per use case, so search engines can index it
- Honest error and empty states; never mock laptops

### Manufacturer scrapers → Scrapling

**Scope:** every manufacturer laptop scraper (the admin catalog scrapers that fill `raw_scrap_laptops`). **Not in scope:** YouTube transcripts (`youtube-transcript-api`, an API) and `search_malaysian_market_price` (SerpApi, an API).

**Why Scrapling** (https://github.com/D4Vinci/Scrapling, BSD-3-Clause)
- Pick the lightest fetcher per site: `Fetcher` (plain HTTP, no browser) when the data is in the HTML, `DynamicFetcher` (Playwright) when JavaScript must run, `StealthyFetcher` when there is bot protection. Today every site likely goes through a full browser
- Adaptive selectors can relocate elements after a site redesign, which is the main way these scrapers break
- Same tool as FinLens (Bursa PDF downloads), so one scraping stack across projects

**Check before switching** (read-only survey first)
- Inventory of current scrapers: site, library, how data is located, how triggered
- Recommended fetcher per site, based on one real request each
- Where adaptive selector signatures are stored, and whether that survives where the scrapers run (Render wipes its disk on each deploy; GitHub Actions runners start clean every run)
- Docker image size with `scrapling[fetchers]` and its browsers, and whether the existing Playwright install can go
- Known pain points: redesigns that broke scrapers, blocking, fields often missing (e.g. Acer battery capacity)

**Note:** Scrapling's own disclaimer asks users to follow site terms and robots.txt. The scrapers only collect public specs and prices from manufacturer sites.

### Scheduled scraping + notifications

**Where and how often:** GitHub Actions, weekly. First, a block test: a temporary `workflow_dispatch`-only workflow on a test branch fetches one page per manufacturer site with the planned Scrapling fetcher, with no database writes and no secrets, and checks the real laptop data is present (a 200 Cloudflare/captcha page counts as blocked).

**Pipeline per run**
1. Scrape → `raw_scrap_laptops`
2. Process (Gemini) → write straight into `laptops`. Existing safeguards stay: a price of 0 from the processor never overwrites a real price, and `LaptopPriceHistory` keeps the old price
3. New laptops: auto-activate only if they pass every go-live condition below; otherwise they stay inactive
4. Embeddings for new laptops, then PickScore `generate-all` (a new active laptop can shift the ranges for everyone)
5. Update "Last Checked" for every laptop whose price the run confirmed, changed or not
6. Send notifications

**Go-live conditions for a new laptop** (all must pass)
- Has a price (already enforced: an active laptop with no price is forced to inactive)
- CPU and GPU both resolve to a benchmark, so PickScore is not withheld
- Core specs present: RAM, storage, screen size, weight
- Battery capacity is not 0
- Assigned to a family (dedup depends on it)
- Not a duplicate of an existing laptop
- Embedding generated (otherwise the agent can't find it)

A laptop that fails stays inactive, and the notification says which condition failed.

**Notifications (new feature)**
- Email through Brevo (already set up for auth emails) + in-app notifications in the admin
- Recipient: Jack (admin) only for now. Design the table so user notifications can be added later without a rewrite
- Events: scheduled run failed; a site returned no data (likely a redesign or block); a price changed by more than ~20%; new laptops that went live; new laptops held back and why; run summary; a configured LLM returned 404 (model retired or renamed by Google; a 404 does not trigger fallback, so that feature stays broken until the setting is changed)

### Price
- Details page shows the official price + a "Last Checked" date. Price history only records changes, so a separate last-checked timestamp is needed; until scheduled scraping (step 9) is live it reflects the last manual scrape
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

- Known gap from Review B1, not scheduled: when a laptop is moved to another family (`POST /families/laptops/move`), its review chunks keep the old `family_id` (review links have the same gap). Fix would be to update that laptop's chunks and links in the move
- What Pico's opening on the compare page contains, and whether it is also the same for everyone (cacheable per set of laptops)
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
