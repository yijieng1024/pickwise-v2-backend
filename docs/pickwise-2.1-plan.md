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
| Where fallback applies | Agent, recommendation, review processor, and (since 2026-10-29) the eval judge, which records which model judged each item. **Not** embeddings (vectors and the 0.53 gating threshold are tied to one embedding model) |
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
| 1 ✅ | Connection pool fix + LLM timeout + fallback + admin switch | Fixes the production QueuePool exhaustion at its root (Gemini has no timeout, and transactions stay open across slow calls), and every later step that calls an LLM benefits from fallback |
| 2 ✅ | Review A: fix the foundation + drop `matched_laptop_id` | Everything after depends on it. Scaling up on dirty data only adds more dirty data |
| 3 ✅ | Review B: read by family + public endpoint + details page | The manual links already done (26 videos) become useful immediately |
| 4 | Review C: which-laptop check, Gemma scope, topic tags, sentiment fix, Laya comparison | Moved up on 2026-10-23: the Review B audit found claims about entirely different laptops (from comparison videos) now visible on public details pages, which the "configuration not stated" label doesn't cover. Also cheaper now: every chunk is re-run once, and the chunk count only grows |
| 5 | Price: official price + date + search links | Small, and XAI D needs the date |
| 6 | XAI A: show the PickScore breakdown in general mode too | Extends something that already exists; small change |
| 6b | Benchmark name matching fix (Snapdragon part numbers + missing integrated-GPU map entries) | Added 2026-10-25. A survey found 32 of 333 active laptops with an unresolved CPU or GPU, every part already in the benchmark table; the names just don't connect. Small, and fixes wrong scores now, so it goes before the larger steps |
| 7 | Entry-aware Pico + open access + temporary chat | The details and compare pages already exist; this makes them useful, and opens Pico to visitors without an account. Before feedback, so the feedback reflects the real entry points |
| 8 | Use-case ranking pages | Needs the PickScore breakdown (step 6) and the "Ask Pico" entry (step 7), so it is built once, complete, right after them |
| 9 | Thumbs up / down | The earlier it ships the better, since feedback data takes time to build up |
| 10 | Scrapers → Scrapling (manufacturer + PassMark benchmarks) + benchmark date | Software compatibility needs more models under RM4000, which means scraping new laptops; switch the scrapers first so that work uses the new ones. PassMark added 2026-10-25 |
| 11 | Scheduled scraping + notifications + background jobs | Keeps the catalog current. After Scrapling, so the old scrapers aren't scheduled and then replaced. Background jobs belong here because the scheduled pipeline needs somewhere to record each stage's status |
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
- Eval judge: reversed 2026-10-29, it now falls back too (see the model decisions under Manufacturer scrapers Phase 1)
- No fallback: embeddings (switching would require re-embedding the whole catalog and re-deriving the 0.53 gating threshold; the existing relational fallback covers embedding outages)

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
  - Same problem, found in the Review B audit (2026-10-23): `IOZrScopvLE` compares the ASUS V16, Zephyrus G16 and TUF but is linked only to the Zephyrus G16 family, so "the ASUS V16 … 9 hours of battery life" shows on G16 pages; `lotIrC6zzmU` (Predator Helios Neo 16) includes a Razer Blade 16 Cinebench claim
- Review B audit baseline (2026-10-23, read-only): 601 chunks, 220 family-scope. Of the family-scope chunks, 8 (4%) carry a numeric performance claim (frame rate, temperature, battery hours, named benchmark) and 39 (18%) mention a performance topic without a number; some name a specific GPU (e.g. an RTX 5090 claim shown on all 10 ROG Strix 18 configurations). English-only regex, so 8 is a lower bound. 2 of the 8 numeric claims are about a different laptop entirely, which the "configuration not stated" label does not cover. Re-measure after Review C
- The same call assigns `scope` (family vs config), replacing the rule-based scope set in Review B. Every existing chunk is re-run once through the new call
- Compare against Laya during the accuracy check: run the same hand-labelled sample through Gemma and through Laya's fine-tuned `laya-typed-decisions` checkpoint (https://github.com/NandhaKishorM/laya, Apache 2.0, self-hosted, `pip install laya`, multilingual routing for Chinese titles/transcripts), for sentiment, topic and scope. Compare accuracy and speed. If Laya is clearly better, or about as accurate and much faster, consider using it for the classification part (Gemma still does the paraphrase). Note: Laya's confidence scores need calibrating on our own data, and it cannot decide which laptop a claim is about (open-ended catalog), so that stays with Gemma
- The same call adds a topic tag (thermals, build, display, battery, performance, other)
- Improve the strength / weakness / neutral label in the same call. Seen on 2026-10-22: a chunk whose main point was a weakness ("8GB will struggle with longevity") was labelled STRENGTH. Check accuracy on a sample of chunks before and after the change
- Uses: group reviews by topic on the details page; later, the Build / Thermals / Display axes on the compare page radar chart (shown separately, not part of PickScore)

**C — decisions and results (2026-10-23 / 24)**
- Design (C0/C1): re-process from the transcript windows, not the existing paraphrases (only 60% of paraphrases still named a laptop). One Gemma call per window returns summary, sentiment, topic, scope and which laptop; candidates are the review's linked families and their separable configurations, plus OTHER / SEVERAL / NONE. An answer naming an id that wasn't offered fails the window (never guessed); `OTHER (name)` / `SEVERAL (names)` is also accepted and the name moved to `other_laptop`
- Topics: thermals, build, display, battery, performance, portability (weight, size), price, other (ports go under other). Price-topic claims are kept out of the public endpoint and `get_review_evidence`, because reviews mostly quote US prices
- A configuration-dependent claim with no known configuration is NOT attached (reason `config_unknown`), replacing Review B's interim "configuration not stated" label for such claims
- Claims about laptops outside the review's links go to `review_unattached_claims` (nothing public reads it). Multi-family reviews are processable
- Re-processing writes to `review_claims_staging` (attached and unattached together) and swaps in one transaction once every window is complete; re-running the swap rolls back
- Changing a review's links marks it for re-processing; old claims stay public until the new set is written
- `/process-bulk` and `/process/{id}` are paused (`REVIEW_PROCESSING_PAUSED`, default true) until C3 is swapped in; then unpause
- Separability ignores price (linking screen and Review C use the same rule)
- Rubric (shared by prompt and labelling page): sentiment by the main point, mild praise / polite criticism still count, neutral = facts, specs, how-to or general advice; mixed → the side stressed more, neutral only if balanced. Which laptop = the main subject; a laptop only mentioned as a comparison point isn't the subject; OTHER = an unlisted laptop; SEVERAL = two or more compared with no single subject; NONE = not about a specific laptop (intro, sponsor, giveaway, handheld, general advice)
- Accuracy check: hand labels by Jack (40 windows, then a fresh held-out 20, two fields: which laptop and sentiment). First run: no method beat "always F1"; most disagreements turned out to be an unclear rubric, so the rubric was rewritten. Held-out (gemini-3.5-flash, 15 of 20 answered): which laptop 67% vs 53% baseline, sentiment 60% vs 47%, wrong public claims 3 of 7 (current claims: 5 of 11). Weak evidence (small n, one labeller). Gemma result pending a re-run with 504 retried
- Laya: rejected. Fast (~50 ms per window on GPU) but not more accurate, and the worst at keeping claims about other laptops off public pages (6 of 7 wrong). Its venv and checkpoints can be deleted
- Model: the review processor stays on gemma-4-31b-it (14,400 RPD). Gemini Flash is not viable (20 RPD on the free tier). Backups: gemma-4-26b, then flash-lite models (500 RPD each)
- Retries: the eval collector retries 429 / 500 / 503 / 504 (30 s, 60 s, 120 s); the production fallback must treat 504 as unavailable. Data-writing paths still never retry
- Embedding quota: Gemini Embedding has 1,000 RPD, and re-processing ~735 windows needs one embedding each. Run C3 over two days (~350–400 windows per day) so the agent's search keeps its quota
- Go-ahead rule: if Gemma's held-out result is about as good as flash's and better than current claims, run C3
- Remaining known weakness: windows comparing several laptops get attached to the first linked laptop. Moved to Review D (below)

**D. Automated matching (ADR-0013)**
- Match the family first, then the configuration
- Use `tie_width` to decide: auto-accept / human queue / discard
- Once in place, attach the claims flagged in C to the laptop they are about
- Comparison windows: allow one window to produce several claims, one per laptop it evaluates, instead of forcing a single subject (today such windows are either attached to the first linked laptop or dropped as SEVERAL)

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

**PassMark benchmark scraper (added 2026-10-25, Jack approved)**
- Today: `app/benchmark/cpu_scraper.py` and `gpu_scraper.py` launch Playwright Chromium on cpubenchmark.net / videocardbenchmark.net, upsert name + mark, never delete rows. Manual only (admin `POST /scrape/cpu`, `/scrape/gpu`, FastAPI background task). Last run 2026-08-19 (5,985 CPU rows vs 6,059 on PassMark on 2026-10-25)
- Move to Scrapling's plain `Fetcher`: one request returned 200, 1.3 MB, 1.0 s, no challenge, full table in the raw HTML, so no browser is needed. GPU page tested 2026-10-27: 200, 625 KB, 2,869 rows in the raw HTML (tags are upper-case: `<TABLE ID="cputable">`)
- robots.txt allows all user agents with `Content-Signal: search=yes, ai-train=no, use=reference`. PickWise uses the marks to compare and score laptops, not to train a model. Jack accepted this use (2026-10-25)
- Scheduled with the other scrapers in step 11

**Benchmark "last updated" date (Jack approved 2026-10-25)**
- New table `benchmark_refreshes`: source (cpu / gpu), refreshed_at, row_count, optionally PassMark's own "last updated" text. The scraper writes one row per successful run. Needs a migration
- Not per row (each run rewrites all ~6,000 rows, so every row would carry the same date) and not in `app_settings` (that table is for settings an admin changes)
- Show our scrape date, not PassMark's (they update daily; our marks are only as fresh as our last scrape): one footnote under the factor list in `PickScoreBreakdown`, e.g. "CPU benchmarks last updated: Aug 2026", covering CPU and GPU. Appears on the details card and the chat/home popovers

**Survey result (Jack, 2026-10-27)**
- No site needs a browser. Apple, HP, ASUS, ROG and PassMark all work with Scrapling's plain `Fetcher`; Acer stays on manual HTML upload (Akamai refuses both `Fetcher` and `StealthyFetcher`). Once every scraper is switched, Playwright, patchright and Chromium come out of the image (about 715 MB of browser plus the `--with-deps` apt libraries), and the project depends on plain `scrapling` + `curl_cffi`, not the `[fetchers]` extra (which also needs playwright ≥1.62, against our 1.60 pin). *Superseded (Jack, 2026-10-28): install `scrapling[fetchers]` and keep the browser, so `StealthyFetcher` stays available; see the implementation plan below*
- Scrapling 0.5 has no `DynamicFetcher`; only `Fetcher` and `StealthyFetcher`. Adaptive selectors are not used: their signatures live in a SQLite file in site-packages (lost on every Render deploy and Actions run), and ASUS/ROG read JSON, not DOM

| Site | Discovery | Specs | Price |
|---|---|---|---|
| Apple | `/my/mac/` page (whole line, no pagination) | spec page, `.techspecs-section` | none today |
| HP | listing `?p=1..9` (121 products, 15 a page) | product page `.product-spec-swapper` | product page (`dataLayer` / `finalPrice`) |
| ASUS | `SeriesFilterResult` JSON API | techspec page, Nuxt 3 `__NUXT_DATA__` JSON (`pinia.pdPageStore.PDTechSpecM2List`) | guide PDF only |
| ROG | same `SeriesFilterResult` API | spec page, Nuxt 2 `window.__NUXT__` literal read by a static parser (never executed) | spec page per-SKU `price`; guide PDF when it is 0 |
| PassMark | – | CPU and GPU list pages, `#cputable` | – |
| Acer | manual HTML upload | uploaded HTML | uploaded HTML |

- **The current ASUS scraper is broken.** www.asus.com moved to Nuxt 3, so `window.__NUXT__.state.PDPage` no longer exists; called directly on a real techspec page it returns `window.__NUXT__.state.PDPage not found`. Every www.asus.com target fails today; ROG (still Nuxt 2) is unaffected
- **ASUS discovery uses `SeriesFilterResult`** (`odinapi.asus.com/recent-data/apiv2/SeriesFilterResult?PageIndex=N&PageSize=20&ProductLevel1Code=laptops…`): 130 laptops in 7 pages, ROG included (`isRogFlag`), each with `ProductURL`, `ProductID`, `M1Id`. Plain request, no key, cookie or header. The listing HTML has no product links even in a real browser, which is what the scroll-based crawler was missing. Entries are model pages, not SKUs, and `PartNo` is empty
- **ASUS prices, and ROG prices the spec page does not have, come from the ASUS Malaysia product guide PDFs** (consumer, ROG, business). An admin uploads the PDFs by hand each quarter; the system extracts SKU, price and specs. The guide's date is recorded as the upload date, because the PDFs carry no date or validity (only an edition on the cover). The guide links are signed CloudFront URLs that expire within a day, so they cannot be fetched automatically. Q3 2026 consumer + ROG guides: 162 laptop SKUs, 160 with an RM price, where `GetPrice` priced 3 of 20 models
- **ROG prices come from the ROG spec page first** (Jack, 2026-10-27): `window.__NUXT__.state.Spec.spec[]` carries a per-SKU `price` (SCAR 18: G835L-WGSA478WP 30999, G835L-XGTQ284WP 32999), unlike `GetPrice`'s shared "starting at" price. When it is 0 (unknown, e.g. G835L-WGSA378W), the price is looked up in the guide; if neither has one, it stays empty and Jack is notified
- **ASUS (non-ROG) prices come only from the product guide PDFs. `GetPrice` is NOT used as a price source** (Jack, 2026-10-27): it returns a shop page's "starting at" price, which can be shared by several models (the P14 and P16 RTX Spark share one page), can be for a configurable product, and can stay after the product is out of stock (P1403: `GetPrice` RM3,799, listing RM4,299, business guide RM4,399)
  - Reason, from the `GetPrice` check (2026-10-27): both RTX Spark ids (31148, 31150) return RM18,999 with the same `BuyLink`, `shop.asus.com/my/asus-proart-p14-p16-h7407b-h7607b-proart-rtx-spark-n1x-cnf.html`, `Customizable_Tag: Configurable` and `StartingAtWording: "ASUS estore price starting at"`. The asus.com model pages show no price; 18999 appears only in their hidden schema.org JSON-LD `Offer`. The listing's `SortPrice` says 29,999. P1403's `GetPrice` RM3,799 is marked "Notify Me" / `OutOfStock` and matches no configuration we can see (spec page: i7-13620H / 16GB / 1TB; guide: P1403CVA-S61159X, i5-13420H / 8GB / 512GB, RM4,399). shop.asus.com is behind DataDome (403 to a plain request), so the configuration a shop price buys cannot be read
- Laptops found on the ASUS website are still added with their specs. Their price is looked up in the latest uploaded guide by SKU or model code
- If no guide has a price, the price stays empty, so the existing rule keeps the laptop inactive, and Jack is notified that it needs a price by hand
- **Chromebooks are ignored** (no longer sold)
- **ROG guide pages have no ruled tables.** pdfplumber's table extraction finds nothing and its text strategy splits words (`RM16,999 R`, `M14,9`), so SKUs are paired with prices by word position. That extraction must be checked by hand against a few pages before it is trusted. In the consumer guide one column often lists several SKUs (colour variants) under one price

**Implementation plan (drafted 2026-10-28; Phase 0 approved by Jack 2026-10-28, later phases still need approval)**

Phases 0–3 in Jack's order (Phase 4 cancelled, see below). Each phase ends with a report to Jack; the next phase starts only after Jack approves it. Shared rules for every phase:
- Nothing in this plan writes to production. Real-data checks either call the scraper functions directly (they return dicts and touch no database), or run the pipeline against the test database with `DATABASE_URL` set explicitly to `TEST_DATABASE_URL` for that one command. That override matters: the app reads `DATABASE_URL`, and a developer `.env` points it at production. Any script that touches a database refuses to start when the URL's project ref is `PRODUCTION_DB_REF`, using the same check as the integration tier
- One small read-only tool for every phase: `python -m app.scripts.scrape_preview <brand> <url>...` runs the brand's scrape function and prints the result as JSON. No session, no database, no LLM. Acer is excluded (it reads uploaded HTML from the database)
- Fetching uses Scrapling's `Fetcher` for every site, as the survey tested. `StealthyFetcher` (the browser) stays installed for any site that starts blocking plain HTTP later; switching one site to it is a one-line change in that site's `fetch`
- Parsing is separated from fetching. Each scraper becomes `fetch` (Scrapling `Fetcher`, a few lines) plus a pure `parse_*(html) -> list[dict]`, so unit tests run the parser on a saved page with no network (`tests/_io_guard.py` blocks the network in the unit tier anyway). Fixtures go in `tests/unit/fixtures/scraper/`, cut down from the survey's `probe/` pages to just the part the parser reads (the `<script>` holding the Nuxt state, the spec section), so they stay small
- Sync `Fetcher` called through `asyncio.to_thread`, not `AsyncFetcher`. The scrape functions stay `async` for their callers. This also avoids curl_cffi's async mode under Windows' Proactor event loop, which is the problem `playwright_utils.py` exists to work around today
- "Prove the test catches the bug" means: write the test, run it green, put the old behaviour back by hand (the specific revert is listed for each test), confirm it goes red with the expected message, then restore the fix. The red output goes in the phase report
- Bulk scraping has no delay between targets today, because the browser was slow enough on its own. Plain HTTP is not. `_process_target`'s loop gets a 1.5 s pause between live fetches (not for Acer)
- One commit per site, so any one site can be reverted alone

**Phase 0: check the dependency before writing code (done 2026-10-28, no commit)**
- Throwaway Python 3.11 venv with our `requirements.txt`. Results: `curl_cffi 0.17.0` needs `cffi>=2.1.1` and `certifi>=2026.10.19` (we pin 2.0.0 and 2026.5.20). `from scrapling.fetchers import Fetcher` fails without patchright, because `scrapling/engines/toolbelt/convertor.py` imports `patchright.sync_api` when it loads, even for the plain HTTP `Fetcher`. `lxml==6.1.1` satisfies Scrapling (`>=6.1.1`)
- Decided from that (Jack, 2026-10-28): install `scrapling[fetchers]` and keep the browser. A pip dry run of the Phase 1 pins below resolves (it also showed `[fetchers]` needs `anyio>=4.14.0`)

**Phase 1: ASUS (www.asus.com), because it is broken now**

*Changes*
- `requirements.txt` (its own commit, first). Added: `scrapling[fetchers]==0.5`, and pins for what it pulls in (`curl_cffi==0.17.0`, `patchright==1.64.3`, `browserforge==1.2.4`, `apify_fingerprint_datapoints==0.15.0`, `msgspec==0.22.0`, `Protego==0.7.0`, `cssselect==1.6.0`, `tld==0.13.2`, `w3lib==2.5.0`, the versions the dry run chose), since the file pins every package. Bumped, with the reason:
  - `cffi` 2.0.0 → 2.1.1: `curl_cffi 0.17.0` requires `cffi>=2.1.1`
  - `certifi` 2026.5.20 → 2026.10.19: `curl_cffi 0.17.0` requires `certifi>=2026.10.19`
  - `playwright` 1.60.0 → 1.64.1: `scrapling[fetchers]` requires `playwright>=1.62.0`. 1.64.1 is the survey's version, and it uses the same Chromium build (revision 1248) as `patchright 1.64.3`, so the two share one browser (see Dockerfile below)
  - `anyio` 4.13.0 → 4.14.0: `scrapling[fetchers]` requires `anyio>=4.14.0`. FastAPI and Starlette run on anyio, so the full unit + golden + integration tiers must pass on this commit
- `Dockerfile`: the install step stays `playwright install --with-deps chromium`, unchanged; it is **not** replaced by `scrapling install`. `scrapling install` runs the same two commands (`python -m playwright install chromium` and `playwright install-deps chromium`), then also fetches the TLD list over the network at build time and writes a marker file into site-packages, which adds nothing for us. Patchright reads the same `PLAYWRIGHT_BROWSERS_PATH` and the same folder layout, and its pinned version wants the same Chromium revision, so it reuses Playwright's build. `patchright install` is never run. Only the comment changes: Chromium is now there for Scrapling's `StealthyFetcher`, and the Playwright scrapers until Phase 2. Check, on a machine with Docker (this PC has none): build the image, and `/opt/playwright-browsers` holds exactly one `chromium-1248` (plus its `chromium_headless_shell-1248`)
- Keeping it to one build later: a unit test reads `browsers.json` from the installed `playwright` and `patchright` packages and asserts the same Chromium revision, so a future bump of only one of them fails CI instead of quietly downloading a second ~700 MB Chromium. Revert: point the test at a copy of patchright's `browsers.json` with the revision changed → fails
- **Cost (accepted by Jack, 2026-10-28):** the image keeps Chromium (~715 MB with its apt libraries) and gains patchright's driver (~110 MB; it ships its own Node driver, like Playwright's)
- `app/scraper/nuxt_state.py` (new): `nuxt3_state(html)`, the `__NUXT_DATA__` reviver from the survey's `nuxt3.py`, made strict. Negative indices are devalue's special values (-1 undefined, -2 hole, -3 NaN, -4/-5 ±Infinity, -6 -0); the survey script read them as Python negative indices, so -1 returned the array's last element. Revive each index once and memoise it (shared references come back as the same object). Raise on a cycle or an unknown type tag, where the survey script quietly stopped at depth 40 and handed back the raw index. Phase 2 adds `nuxt2_state` to the same module (see "nuxt2_parse.py reuse" below)
- `app/scraper/asus_scraper.py`:
  - Discovery: `crawl_asus_specs_links` drops the scroll-and-click Playwright crawl and pages through `SeriesFilterResult` (`PageIndex=1..`, `PageSize=20`, until a page comes back short; a `_MAX_PAGES` ceiling like HP's). It returns each entry's `ProductURL`, normalised (absolute, no query, trailing `/`, `/techspec/` and `/spec/` removed), and drops Chromebooks. If page 1 has no `ProductList`, it **raises** instead of returning `[]`: an empty list surfaces as "No URLs found", which looks like a quiet day rather than an API change. The API URL is a constant in the module, so it is still not client-supplied. The router still requires the brand to have a `base_scrape_url`; the ASUS crawler simply doesn't use it
  - Scrape, www branch only: fetch `…/techspec/`, `nuxt3_state`, read `pinia.pdPageStore.PDTechSpecM2List.TechSpec` (with the same fallbacks the current code tries). The per-SKU mapping (`SpecList`, `Name`, `ImageLink`, `?v=N` suffix) moves unchanged into `parse_asus_techspec(html)`
  - Price: www pages return `"N/A"` always. The `PDPriceList` / `modelPrice` / `modelSkuPrice` lookup and its "single valid price" fallback are deleted, per Jack's 2026-10-27 decision (price comes only from the guides). The ROG branch is untouched in this phase and still runs through Playwright
- Migration: **none**

*Check before any ASUS work: the Playwright bump didn't break the scrapers still on Playwright* (no database)
- After the `requirements.txt` commit, in a local venv with those pins and `playwright install chromium` (the 1.64.1 build): `scrape_preview asus <one ROG URL>`, `scrape_preview hp <one product URL>`, `scrape_preview apple <one Mac spec URL>`. Each must return variants with specs, as before the bump
- PassMark can't go through `scrape_preview`: `run_cpu_list_scraper` launches the browser and writes to the database in the same function. The check is a throwaway script that runs only its browser part (launch, `goto`, `wait_for_selector`, the `evaluate` that reads `#cputable`) and prints the row count, never opening a `Session`. The same for GPU. Counts close to 6,059 CPU / 2,869 GPU
- Any failure stops Phase 1 and is reported before the ASUS changes start

*Tests* (`tests/unit/test_asus_scraper.py`, `tests/unit/test_nuxt_state.py`)
- Real Nuxt 3 techspec fixture → the expected variant count, names, a few spec values and image URLs. Revert: point the parser back at `state.PDPage` → fails with "not found"
- A page with no `__NUXT_DATA__` (the old Nuxt 2 shape, or a challenge page) → raises with a clear message, never `[]` reported as success. Revert: catch and return `[]` → fails
- Reviver: a hand-written payload using -1, a shared reference and `ShallowReactive` → `None`, the same object twice, unwrapped value. Revert: copy in the survey's `d()` → the -1 case returns the last element and fails
- Discovery parser on two saved `SeriesFilterResult` pages: URLs normalised, Chromebooks gone, ROG entries kept, and an empty `ProductList` on page 1 raises. Revert: drop the Chromebook filter → count fails
- No www.asus.com price: a fixture whose state still carries a `PDPriceList` → every variant has `"N/A"`. Revert: put the price lookup back → fails

*Check on real data (no database)*
- `scrape_preview asus` on all 130 discovered URLs (ROG ones included, which still go through Playwright): count of successes, failures with their errors, variants per page. Hand-check 3 www pages against what the techspec page shows in a browser
- Compare discovery with the current queue: Jack exports `GET /scraper/targets?brand_id=<asus>` from the admin as a CSV, and a script compares it with the normalised discovery list: how many URLs are new, already queued, or queued under a different spelling. This matters because the queue dedupes on exact URL. If the same product is spelled two ways, it gets scraped twice, and since the processor's upsert key is the LLM-generated `model_code`, two spellings can become two catalog rows. Also check on the test DB or with Jack: is there a separate `ROG` row in `laptop_brands`? (Dispatch knows only `asus`, so ROG has always been scraped through the ASUS brand.)
- Optional: end to end on the test DB, with brand ASUS seeded: crawl → bulk scrape a handful of targets → `process-pending` on those (this costs Gemma quota) → look at the laptops it creates

*Stop and report*: the Playwright-bump check (one result per scraper), per-page success counts, the queue comparison (new / existing / spelled differently), every failure, and the red output from each reverted test.

*Risks*
- Live site: none until an admin crawls and processes. New ASUS laptops come in with price 0, so the existing mapper rule makes them `inactive`, and they stay hidden until Phase 3 gives them a price. Existing ASUS laptops are not re-scraped (bulk scrape skips URLs already in `raw_scrap_laptops`), and if one were re-processed, a 0 never overwrites a stored price
- Admin: the first ASUS crawl adds ROG URLs to the queue as well. Any that were queued under a different spelling become duplicates. The queue comparison above finds them before Jack crawls
- "Waiting for a price" on the dashboard grows by however many ASUS laptops get processed before Phase 3. That's expected

*Decisions from the Phase 1 report (Jack, 2026-10-29)*
- **Series-only pages stay visible failures** (option a). Two www pages, `asus-tuf-gaming-16-2026` and `asus-expertbook-b5-g2-16-intel`, carry only `PDTechSpec`, whose `SpecList` is a list of `{Title, Content}` rather than a per-SKU dict. `parse_asus_techspec` keeps raising `SpecList is a list, expected a dict`. A bulk run records each as one failed target with that reason (`scrape_status = failed`, no raw row), and the rest of the run goes on; `test_series_only_page_is_a_failed_target_in_a_bulk_run` checks this through `_process_target`. Revert: remove the raise → the page writes a raw row and the test fails
- **No separate ROG brand.** `laptop_brands` has no ROG row, so ROG stays under the `asus` brand and only the one ASUS crawl runs. Nothing to retire
- Queue comparison: run on two Supabase exports of `laptop_scrape_urls` (each capped at 100 rows), 115 of the 116 ASUS rows between them, which Jack accepted as enough. Against 102 discovered URLs: 14 new, 88 already queued, 3 products also queued under another spelling (`/spec/`, `/techspec/`, `/techspec`), 22 gone (6 of them Chromebooks), 3 products already duplicated in the queue (7 rows). The crawl adds no new duplicate: each of the three already has its bare URL queued, and bulk scrape skips on `source_url LIKE url%`
- **Hand check passed** (Jack, 2026-10-29): Vivobook 16 M1607 (2 variants), ExpertBook P5 G2 P5605CAA and ProArt P16 H7607BA match the site field for field. Two pages list two memory configurations under one model (P5605CAA: "32GB" and "32GB x 2" = 64GB; H7607BA: 128GB and 48GB). The scraper is faithful to the page, so no scraper change. How the processor handles two options in one field is a separate question (see the Phase 1 report)

*Model decisions (Jack, 2026-10-29)*
- **Why:** Google retired `gemma-4-26b-it`. `generateContent` on it returns 404 NOT_FOUND, and `models.list` on the project key no longer has it; `gemma-4-26b-a4b-it` is the replacement. It was the first backup in all three chains, and a 404 does not fall back. So the processor chain stopped there instead of reaching its Gemini backups, and the agent and review processor had no working backup at all
- **Replace `gemma-4-26b-it` with `gemma-4-26b-a4b-it` everywhere in code** (commit `c7b18a4`):
  - The `agent`, `review_processor` and `processor` default chains in `app/config.py`, and `MODEL_OPTIONS` in `llm_config.py`
  - The new name keeps the old model's limits (16k TPM / 30 RPM / 14,400 RPD), marked unconfirmed for the new name
  - `test_every_default_chain_model_is_in_the_allowed_list` checks every model in the default chains against `MODEL_OPTIONS`. Putting `gemma-4-26b-it` back in the agent chain alone turns it red
  - The test cannot detect a model Google retires; only `models.list` shows that
  - Jack checks by hand in production for the old name: the LLM Settings rows (`<use>.model` / `<use>.backup_models`, `processor.model`) and any `LLM_*` env var on Render
- **The eval judge falls back** (commit `8092f2e`). This reverses "no fallback for the eval judge":
  - `JUDGE_MODELS` is `gemma-4-31b-it`, then `gemma-4-26b-a4b-it`, built with `build_llm`: the same code and the same retryable errors (429, 5xx, timeout) as the agent
  - Each turn has a `judge_model` field, and each item has `judge_model`, the list of models that judged it. The summary prints how many items each judge model scored
  - A run scored partly by the backup is not comparable with one scored only by the primary
  - Temperature stays at 1.0, the models' own default
- **Multi-option prompt rule, retried on the TEST database** (2026-10-29): rows split as expected, but the rule is **not committed**:
  - P5605CAA gave 2 rows, 32GB and 64GB, `max_ram_gb` 96 on both, matching the page
  - H7607BA gave 2 rows, 128GB and 48GB, but both have `max_ram_gb` 128. The page gives the 48GB line its own "Max Total system memory up to: 48GB"
  - So the model copied the first option's max instead of pairing each option with its own
  - Fallback carried both pages:
    - P5605CAA: `gemma-4-31b-it` 500, then `gemma-4-26b-a4b-it` 504, then `gemini-3.5-flash-lite` answered
    - H7607BA: `gemma-4-31b-it` 503 "high demand", then `gemma-4-26b-a4b-it` answered

**Phase 1b: Docker image Python 3.11 → 3.12** (its own step, after Phase 1 is committed; not started)
- Why: CI runs 3.12 (`ci.yml`), the image runs 3.11 (`FROM python:3.11-slim`), so the tiers pass on a Python production never runs
- `Dockerfile`: base image `python:3.12-slim`
- `requirements.txt`: find any pin that has no 3.12 wheel and bump it, with the reason, as in Phase 1
- Check: the full unit + golden + integration tiers on 3.12, and `python -m app.scripts.seed_channel_fields` (dry run) now runs
- Migration: none

**Phase 2: ROG, HP, Apple, PassMark (one commit each, in that order)**

Before the sites, one `refactor:` commit with no behaviour change: brand dispatch moves to one place (see "Brand dispatch" below).

*2a ROG*
- `nuxt_state.py`: add `nuxt2_state(html)`, the hardened `nuxt2_parse.py`
- `asus_scraper.py` rog branch: fetch `…/spec/`, `nuxt2_state`, read `state.Spec.spec`. The current per-SKU mapping (specContent, mktName + skuName, `price`, skuImg) moves as it is into `parse_rog_spec(html)`. One addition: `specs["SKU"] = skuName`, so the SKU travels with the raw row (Phase 3 matches on it). `price == 0` stays `"N/A"`
- Tests: real ROG fixture → variants with the SKU, per-SKU price (SCAR 18: G835L-WGSA478WP 30999, G835L-XGTQ284WP 32999) and 0 → "N/A" (G835L-WGSA378W). Revert: read the first variant's price for every variant → fails. Parser tests for each hardening fix (unknown identifier raises, a non-empty pre-return body raises, `\x41` decodes to `A`, a surrogate pair decodes to one character). Revert each by copying the scratchpad version back → that case fails
- Real data: run the Playwright scraper (still installed) and the new one on the same 10 ROG URLs and diff the output dicts. Any difference other than the added `SKU` key has to be explained before the commit. The processor reads this text, so a changed value can change a live laptop on its next re-process

*2b HP*
- `hp_scraper.py`: the listing walk `?p=1..` becomes Fetcher + CSS selection of product links. Specs: the `_SPECS_JS` logic (`.product-spec-swapper`, `dl.product-spec-attribute`, section-prefixed keys) rewritten as CSS selection in `parse_hp_product(html)`. Price and part number come from the `dataLayer` push in the inline script (parsed as JSON, not executed), with the `finalPrice` node as the fallback. HP is Magento, like Acer: reuse `acer_scraper._parse_prices` (it reads `data-price-type`, ignoring the empty-type instalment node) and `_parse_image_urls` (the `mage/gallery/gallery` JSON), imported from `acer_scraper` once it's confirmed the HP pages carry the same nodes (the gallery path filter differs: HP uses `/catalog/product/`, Acer `/media/catalog/product/`). The current HP image code reads `.fotorama__img`, which exists only in the live DOM, so the gallery JSON is the source to use in raw HTML
- Tests: product fixture → specs, price, part number, images (accessory images excluded). Revert: read the first price node instead of `finalPrice` → fails on the instalment figure. Listing fixture → product links, and pagination stops on a page with no new links
- Real data: old against new on 10 products plus the full listing count (121 expected); explain every difference

*2c Apple*
- `apple_scraper.py`: `/my/mac/` discovery and `.techspecs-section` text via Fetcher + CSS. The risky part: Playwright's `inner_text()` follows CSS (hidden elements left out, line breaks where blocks end); Scrapling's text extraction does not. The LLM reads this text, so the diff of old against new on every Mac spec page is the gate. The fix is to normalise whitespace, or to drop the specific hidden nodes; never send the processor different text without checking it. Images: compare `img` sources, because in the raw HTML lazy images may sit in `srcset`/`data-src`. `_filter_variant_images` stays as it is
- Tests: spec page fixture → section text that matches a snapshot taken from the old scraper's output (checked by eye once, then frozen); discovery fixture → the expected model links

*2d PassMark + benchmark date*
- `app/benchmark/cpu_scraper.py`, `gpu_scraper.py`: Fetcher + `parse_passmark_table(html, table_id)` (lxml lower-cases tags, so `<TABLE ID="cputable">` is found as `#cputable`). The upsert is unchanged. New: a **row-count floor** (CPU ≥ 5,000, GPU ≥ 2,500). Below it the run raises and writes nothing, because a challenge page returning 200 would otherwise parse as a successful refresh of 0 rows. The two files are the same apart from table and model; merge them only if it's shorter
- Migration: **yes**. Adds `benchmark_refreshes` (`id`, `source` cpu/gpu, `refreshed_at`, `row_count`, nullable `source_updated_text`), as approved 2026-10-25. One row is written after each successful upsert, in the same transaction. Check: upgrade and downgrade on the test DB, and `alembic check` reports no further operations
- Wire up the consumers already planned: `dashboard_router`'s `passmark_refreshed_at` reads the latest row (it is `None` today). The Pick Score response gains `benchmarks_refreshed_at {cpu, gpu}` for the `PickScoreBreakdown` footnote, a frontend change in `pickwise-v2-frontend`
- Tests: real CPU and GPU fixtures (trimmed to a few hundred rows plus the header) → name and mark pairs, commas stripped, non-numeric marks skipped. A trimmed fixture below the floor → raises. Revert: remove the floor → fails. Integration: a successful run writes exactly one `benchmark_refreshes` row, a failed run writes none
- Real data: parse the live pages with no DB (row counts against 6,059 CPU / 2,869 GPU). Then on the test DB: a full upsert and a refresh row

*2e Remove the Playwright scraper code (last commit of Phase 2, after 2a–2d)*
- Nothing calls it once every scraper is on `Fetcher`, so it goes. The `playwright` package, Chromium and the Dockerfile step stay, because Scrapling's `StealthyFetcher` needs them
- Delete `app/scraper/playwright_utils.py` and every leftover Playwright code path: the `_async_*` browser functions in `asus_scraper`, `apple_scraper` and `hp_scraper` (with HP's `_SPECS_JS`), and the `sync_playwright` code in `app/benchmark/cpu_scraper.py` and `gpu_scraper.py`. Update the comment in `app/common/job_service.py` that points at `playwright_utils.py`
- `CLAUDE.md`: remove the "Windows Playwright compatibility" section; update the `app/scraper/` and ASUS dual-path paragraphs (Scrapling `Fetcher`, Nuxt 3 / Nuxt 2 parsers) and the Acer note "`chromium` remains for Apple/ASUS" (it now stays for `StealthyFetcher`). The "Install Playwright browsers" command stays. `deploy.yml`'s comment about the Chromium layer stays true
- Check: no file under `app/` imports `playwright` or `patchright` directly (`rg "playwright|patchright" app/` finds nothing). The full unit + golden + integration tiers stay green; a leftover import of the deleted `playwright_utils` would fail at import time there, and in production it would stop the whole API from starting, since `app.main` imports the scraper routers. Then `scrape_preview` once more for each site
- No migration

*Stop and report*: per site, the old against new diffs and how each difference was resolved, row counts, the migration up/down output, the 2e check, and the red output from each reverted test.

*Risks*
- Live site: changed raw text can change specs when a laptop is re-processed. The old against new diffs are the guard. A PassMark refresh moves marks, so after the next `generate-all` Pick Scores move as well (expected, since factors are percentiles). Run `python -m app.scripts.dump_pickscore` before and after on the test DB
- Admin: no endpoint shapes change. Scrapes get much faster, which is why the pause between targets is added

**Phase 3: product guide import and price extraction (consumer, ROG, business)**

*Where the PDF goes.* The Q3 guides are **84 MB (consumer), 158 MB (ROG) and 258 MB (business)**. Storing them in Postgres (Supabase free tier: 500 MB) or uploading and parsing them on Render's free instance (512 MB RAM, a disk wiped on every deploy) isn't workable. **Decided (Jack, 2026-10-28): the PDF is stored in Google Drive and extracted in GitHub Actions.**
1. Jack puts the PDF in Drive, either directly or from a new page in the admin. The admin upload goes from the browser straight to Drive through a resumable upload session the backend creates; the file never passes through the backend (Render has 512 MB RAM)
2. A GitHub Actions workflow reads the PDF from Drive through a Google service account and runs `python -m app.scripts.extract_price_guide <pdf> --kind consumer|rog|business --out rows.json`. The script itself touches no database and no network. It finds the price pages by content (pages with both `RM` amounts and SKU-shaped words, as the survey overview counted), not by the hard-coded page ranges the survey scripts used. For each SKU it extracts `sku`, `price_rm` (or null), `page`, and where the page gives them, `cpu`, `ram_gb`, `ssd_gb`. It also prints a per-page summary for the hand check
3. The extracted rows reach the admin "Price guides" page, where Jack gets the match preview and then applies it (unchanged from here on)
4. Stored: `price_guides` (`id`, `kind`, `filename`, `pdf_sha256`, `edition_text` from the cover, `uploaded_at` = the guide's date, `uploaded_by`, `row_count`) and `price_guide_rows` (`guide_id`, `sku`, `model_code` = the part of the SKU before the hyphen, `cpu`, `ram_gb`, `ssd_gb`, `price_rm` nullable, `page`). The sha256 identifies the exact PDF a price came from, without storing the PDF itself
- **Short survey at the start of Phase 3 (not before):**
  - A service account may have no storage quota of its own, so uploading through it into Jack's Drive could fail; reading a file shared with it should work. Check both. If upload fails, the admin upload uses Jack's own OAuth and the service account only reads
  - The resumable upload session: the backend creates it and hands the session URL to the browser, so a 258 MB file never goes through Render
  - How the admin triggers the workflow (for example `workflow_dispatch` through the GitHub API) and how the extracted rows get back to the preview
- The hand check of 3 pages per guide on each new edition stays

*How a guide row is matched to a catalog row* (`app/laptops/price_guide.py`, one pure function `match(guide_rows, catalog_rows) -> MatchResult`, so the whole decision is unit-tested with no database)
- **By SKU (ROG):** the guide's `sku` equals the laptop's `manufacturer_sku`, compared case-insensitively after trimming. A new nullable column, `laptops.manufacturer_sku`, holds it. The processor fills it **deterministically**, not through the LLM: when a raw row carries `specs["SKU"]` (Phase 2a) and its extraction produced exactly one variant, that variant gets the SKU. When there are several variants, it stays null, because which one is the SKU would be a guess. It is never overwritten on update once set
- **By model code + specs (ASUS, and ROG rows with no SKU):** candidates are ASUS-brand laptops whose `model_code` or `product_name` contains the guide row's model code (`P1403CVA`, falling back to the base `P1403`). The CPU part number must match (`i5-13420H` against `cpu_model` "Intel Core i5-13420H", compared through the part-number normalisation added in 6b, not fuzzy), and `ram_gb` and `ssd_gb` must be equal. A guide row with no CPU, RAM or storage cannot use this path
- **Outcomes**, each listed in the preview:
  - `matched`: exactly one catalog row, and no other guide row gives that row a different price. Several SKUs mapping to one row at the **same** price (colour variants) count as matched
  - `ambiguous`: more than one catalog row fits (for example, same CPU/RAM/SSD but a different GPU or panel). The candidates are listed
  - `conflict`: one catalog row is claimed by guide rows with different prices
  - `unmatched_guide`: no catalog row fits. The SKU is in the guide but not in the catalog, so the list is a crawl or process worklist
  - `unpriced`: the guide row has no price
  - Catalog ASUS rows the guide doesn't cover are listed too: the "needs a price by hand" list
- **When a match is unclear, nothing is written.** Ambiguous and conflicting rows are shown with their candidates, and Jack sets the price by hand through the existing laptop edit (`PUT /laptops/{id}`). No best guess, no fuzzy fallback. A guessed row puts a wrong price on the live site, while an unmatched row only stays in a visible list. This is the same reasoning as family matching, where a guessed id is worse than a null one

*Applying* (`POST /admin/price-guides/{id}/apply`, preview = `GET …/preview`; apply runs the same `match` and writes only `matched`)
- Fill rule: a guide price is written when the stored price is 0, or when the stored price itself came from a guide. A ROG price from the spec page is never overwritten by a guide (Jack: spec page first). An admin-entered price is never overwritten either; it is listed as "differs from guide"
- To know where a price came from: two nullable columns on `laptop_price_history`, `source` (`page` / `guide` / `manual`) and `price_guide_id`. The processor writes `page`, `PUT /laptops/{id}` writes `manual`, and the import writes `guide`. "Came from a guide" means the latest history row's `source = 'guide'`. Older rows stay null, which counts as "not a guide". **Decided (Jack, 2026-10-28):** a price with no recorded source is never overwritten by a guide; it is listed as "differs from guide" for Jack to decide. This is also the price source step 15 (XAI D) needs
- Writes go through the ORM like `PUT /laptops/{id}`: a price-history row only when the price changes, `price_checked_at` set to the apply time even when unchanged ("Last Checked" means the guide confirmed it), and the mapper event and `updated_at` stamp as usual. Restoring a price does **not** make a laptop active again (existing rule). The preview lists "priced by this guide but inactive", so Jack can activate them; automatic go-live is step 11
- One transaction per apply, run as a background job (`background_jobs`), so a failed apply writes nothing. Re-applying the same guide changes nothing; the test below checks this
- Step 11's scheduled pipeline calls the same apply function for the latest guide of each kind after every process run, so laptops found later still get priced. That is the "looked up in the latest uploaded guide" rule, in one place

*Migration*: **yes**. Adds `price_guides`, `price_guide_rows`, `laptops.manufacturer_sku` (nullable, with a partial unique index where not null), and `laptop_price_history.source` + `price_guide_id` (nullable, FK to `price_guides`). Every new `laptops` and `laptop_price_history` column is nullable with no backfill, so existing rows need no rewrite. Upgrade and downgrade on the test DB, plus `alembic check`. New table modules are imported in `alembic/env.py` (the data-loss guard)

*Tests*
- Word pairing (`pair_skus_with_prices(words)`, pure, takes pdfplumber word dicts): fixtures are word lists dumped from 3 real pages per guide kind as JSON (text and coordinates only, no PDF in the repo), with the expected SKU → price pairs **checked by hand against the printed page first**. Includes the split-word ROG case and a consumer column with several SKUs under one price. Revert: drop the same-column (x-distance) condition → a SKU takes the neighbouring column's price and the test fails
- `match`: SKU exact; model code + specs; colour variants at one price → matched; two catalog rows that fit → ambiguous, nothing written; two guide prices for one row → conflict; a CPU differing only by suffix (`i7-13620H` against `i7-13620HX`) → not matched. Revert each by loosening the rule (accept the first candidate; compare CPU fuzzily) → the matching case fails
- Processor `manufacturer_sku`: one variant → set; two variants → null; update → not overwritten. Revert: set it on every variant → fails
- Apply (integration tier): the fill rule (0 filled; guide-sourced price overwritten; page and manual prices not overwritten); a history row with `source='guide'` only on change; `price_checked_at` stamped when unchanged; apply twice → the second run writes nothing; an inactive laptop stays inactive. Revert: drop the source check → the page-price case fails
- Real data: run `extract_price_guide` on the three Q3 PDFs. Hand-check 3 pages per guide against the printed page (the 2026-10-27 survey found 162 laptop SKUs / 160 with a price in consumer + ROG, which is the baseline). Then on the **test DB**, seeded with the ASUS/ROG laptops Phases 1 and 2 produced there, upload and preview. Report the outcome counts and every ambiguous or conflict row. No apply against production in this plan

*Stop and report*: hand-check results per page, preview counts per guide, the ambiguous and conflict lists, migration output, and the red output from each reverted test. Jack decides whether the matching rules are tight enough before anything is applied for real.

*Risks*
- Live site: apply writes prices that are visible right away, and a wrong match is a wrong price on a public page. Hence preview first, exact rules, nothing written when unclear, one transaction. Price factors change only after the next `generate-all`
- Admin: a new page (frontend repo, `src/app/admin/`, in the catalog group). "Waiting for a price" drops after an apply. Prices Jack types in by hand are safe, because `manual` is never overwritten
- The guide layout may change in the next edition: the hand check of 3 pages per guide is repeated for every new edition, and the preview's `unmatched_guide` count jumping is the visible sign

**Phase 4: remove Playwright and Chromium. Cancelled (Jack, 2026-10-28): keep the browser so StealthyFetcher stays available.**
- The one part kept, deleting `playwright_utils.py` and the leftover Playwright scraper code once nothing calls it, moved to the end of Phase 2 (2e)
- Cost of keeping the browser: see the Phase 1 `requirements.txt` and Dockerfile notes (Chromium ~715 MB stays, patchright's driver adds ~110 MB)

**Brand dispatch: one place**
- ASUS discovery and ROG coming from `SeriesFilterResult` **does not change the brand key**: both are the `asus` brand, and the www/ROG split was already a host branch inside `asus_scraper` (dispatch has never known a "rog"). What changes is that one ASUS crawl now returns ROG URLs as well, so if a separate ROG crawl or brand row exists, it must stop being used (checked in Phase 1)
- The three `if/elif`s (`router.py` feed-crawler and scrape-url, `bulk_scraper._dispatch_scraper`) become one dict in `bulk_scraper.py`, where `SCRAPER_BRANDS` already lives: `SCRAPERS = {"apple": (crawl, scrape), "asus": …, "acer": …, "hp": …}`, with `SCRAPER_BRANDS = frozenset(SCRAPERS)`. Each entry has the same signature: `crawl(start_url, session, brand_id) -> list[str]` and `scrape(url, brand_id, session) -> list[dict]`. Apple's single dict is wrapped once there, and brands that don't need a session ignore it. Both routes and the bulk path call `SCRAPERS.get(name)`. `None` gives the same 400 / `ValueError` as today
- `tests/unit/test_scraper_brands.py` reads `_dispatch_scraper`'s source with a regex; that test goes away, since the dict *is* `SCRAPER_BRANDS`. Replace it with one test that a brand missing from `SCRAPERS` gets 400 from both routes. Revert: put back an `elif` in one route → that route's brand is still dispatched while the dict lacks it, and the test fails
- Done as a `refactor:` commit at the start of Phase 2, not inside the Phase 1 fix, so the fix for the broken site stays small

**`nuxt2_parse.py` reuse (from the 2026-10-27 survey scratchpad)**
- Yes, as the basis for `nuxt_state.nuxt2_state`. It reads the `window.__NUXT__=(function(…){return …}(…))` literal without executing it, so the image needs no JavaScript runtime. It already handles the parameter/argument binding, shorthand keys, `void 0`, and `Array(n)`
- It needs hardening before it reads production pages:
  1. Unknown identifiers return `None` silently (`env.get(w)`); they must raise
  2. Statements before `return` are captured and thrown away. Nuxt 2 puts assignments there (`a.b=c`) for shared values, so data can be lost silently. Raise when that part is not empty, and only add support for simple `x.y=z` if a real page needs it
  3. `\x` escapes decode wrongly (`\x41` becomes `x41`), and a `\u` surrogate pair becomes two lone surrogates
  4. `NaN`, `Infinity` and hex numbers aren't handled
  5. Brace matching ignores template literals and regex literals (acceptable for Nuxt data, but a mismatch should raise rather than parse garbage)
  6. The `__main__` block and file output go
- Each fix gets its own case in `tests/unit/test_nuxt_state.py`, plus the real ROG fixture

### Admin portal (decided 2026-10-25)

Survey 2026-10-25 (read-only, from code): 22 pages in 8 sidebar groups, under `src/app/admin/` in the frontend. A `background_jobs` table, `/admin/jobs` page and running-jobs pill already exist; LLM settings and monitoring are complete; notifications don't exist.

**First: fix the bugs** (Jack, 2026-10-25) ✅ shipped 2026-10-25 (edit page uncached, retire → Suspended, inline status uses the server's answer, review buttons disabled while paused via `process.paused` on `GET /reviews/pipeline-status`). PassMark button: works in principle but leaves no record; left for step 10
- Laptop edit page can show stale data (server component, `apiFetch` caches 60 s), so saving again could write old values back
- Retiring a laptop offers "Set to Inactive"; retiring means `suspended` (inactive is the awaiting-a-price queue)
- Inline status change ignores the server's response (an unpriced laptop set to Active shows "now active" while the backend keeps it inactive)
- "Run PassMark scraper": the "Refresh started" alert and its Recalculate button never appear; Jack reports the scrape doesn't work at all, to be checked
- Review buttons stay enabled while review processing is paused, and only a 409 toast says so

**Then: tidy and restructure** (Jack: the whole thing is messy; some things are missing where they should be and present where they shouldn't be)
- Sidebar (decided 2026-10-25) ✅ shipped 2026-10-26. Labels and headings in Title Case:
  - Pinned at the top, always visible: Dashboard, Jobs
  - Below them, a dropdown of groups; choosing a group shows only that group's pages. Opening a page from elsewhere switches the dropdown to its group; the last chosen group is remembered
  - Data Collect: Brands & Sources, Scrape Queue, Acer Upload, Raw Records, AI Clean-Up (moved from Catalog: it is part of the collection flow), Embeddings
  - Catalog: Laptops, Families, Upgrade options, Types & categories
  - Scoring & Search: CPU Benchmarks, GPU Benchmarks. Later a new PickScore page (last calculated, withheld and Unverified laptops, Recalculate). Group name to revisit: with Embeddings moved out there is no "Search" left
  - Reviews: Review pipeline, Sources, Link reviews (Match queue merged into Link reviews, the family-first ADR-0012 design). Match queue moves whole into an "All reviews" tab on Link reviews, keeping its laptop-first "Match to laptop…" dialog for now (unifying it needs backend work; clean up later). Rematch pending moves to Review pipeline beside Run ingest. The old /admin/reviews/raw URL is removed with no redirect (Jack, 2026-10-25)
  - Users: Users, Questionnaire
  - Pico & System: Chatbot monitoring, LLM settings, Token usage
- Notifications: a bell in the admin topbar next to the avatar, opening a panel like YouTube's (unread dot, "Important" section then "More notifications", time ago, a settings gear). Not in the sidebar. Built with the notifications feature in step 11
- Dashboard: redesign completely
- Move features to where they belong; remove duplicates (two ways to link a review, processor model in two places); fix stale copy; confirmations for actions that change the live site or cost quota; one type scale instead of ~220 arbitrary pixel sizes
- Principle: each page starts from the question it answers, then shows what that needs

**Dashboard (Jack approved 2026-10-26).** Answers: is anything broken, does anything need me, is the data fresh?
1. Needs attention (top): only problems, else "All good", each with a link to the page that fixes it: failed jobs (7 days), review processing paused (and why), laptops with no PickScore or Unverified factors, inactive laptops waiting for a price, reviews waiting to be linked, Pico error or fallback rate high; after step 11 also new laptops held back and a retired LLM model
2. Freshness, as "how long ago", amber/red when old: last scrape per brand, PassMark refresh, PickScore recalculation, embeddings, oldest price "Last Checked". Thresholds: price Last Checked amber > 14 days, red > 30; benchmarks amber > 30 days, red > 60; PickScore amber when older than the latest catalog change
3. Running now + the last 5 jobs
4. Catalog: active / inactive / suspended counts; share with PickScore, embedding, reviews
5. Pico: conversations today / 7 days, error rate, link to Chatbot Monitoring
- Removed: the action buttons (Recalculate PickScores, Generate embeddings). They move to their own pages

**Review Pipeline page (Jack approved 2026-10-26).** Answers: where are the reviews, where are they stuck and why, what do I do next?
1. Status banner: paused or running, with the reason
2. Funnel: videos found → transcript → linked → processed into claims → shown on the site, with the stuck count and reason at each step, each count opening the list
3. Actions beside their step: Run ingest + Rematch pending; Retry transcripts (new button, today curl only); Run processing (disabled while paused). Each shows the quota cost and asks to confirm
4. Coverage: families with / without reviews; share of active laptops with visible reviews
5. Quota: today's review-processing usage per model and what's left
6. Recent runs: when, succeeded, failed, why
- Removed: Aggregate (the public page and Pico read chunks directly; the summary table is unused)

Data survey (2026-10-26, read-only): most items can be built from stored data with new queries. Decisions (Jack, 2026-10-26, following the recommendations):
- Add `laptops.updated_at` (migration, backfilled from `created_at`, set in the existing mapper event) for the "latest catalog change" PickScore rule. Side effect accepted: the "price still right" button counts as a change
- Review pause reason stored in `app_settings` (no migration), editable in the admin; the pause flag itself stays an env var
- Review ingest, processing (bulk and single), retry transcripts and rematch become background jobs now (moved up from step 11): this gives "recent runs" and stores per-review failure reasons in the job's errors. These endpoints change to 202 + poll
- Usage logging: record embeddings as an LLM usage feature (shared 1,000/day with Pico search); fix `reprocess_reviews.py` building its chain with `feature=None`. YouTube API units: not recorded for now; show the next run's estimated cost only
- PassMark refresh date shows "unknown" until step 10 adds `benchmark_refreshes`
- Count rules: "waiting for a price" = inactive with price 0; review coverage uses the same three tiers as the details page; "today" for Google quotas uses the Pacific-time reset
- One summary endpoint per page (`GET /admin/dashboard/summary`, extended `GET /reviews/pipeline-status`), SQL aggregates only
- Build order: backend first (plan, then build), then frontend
- ✅ Shipped 2026-10-26: backend (summary endpoints, `laptops.updated_at`, review jobs, usage logging, trends block) and frontend (Dashboard and Review Pipeline rebuilt from the design in `docs/admin-redesign/`, one request per page, per-section error boundaries, confirm-with-cost before every run, Recalculate PickScores moved to the CPU/GPU Benchmarks headers). Chart palette in DESIGN.md is now one palette for the admin (light and dark steps), with "View as table" on every chart and fixed brand colours
- Found on the way: the unit tier had been making real embedding calls and writing usage rows to production (12 rows, deleted 2026-10-26). A guard now fails any unit test that opens a non-SQLite database or a non-loopback network connection
- Never run the backend locally: its `.env` points at production, so startup would reset production jobs and run migrations against production
- Later: Aggregate cleanup (endpoints, aggregator, model, `laptop_review_summary` table) once nothing uses it

**Naming rule (Jack, 2026-10-26):** "Pick Score" is two words in every piece of user-visible text, public site and admin (headings, labels, buttons, tooltips, Pico's replies, emails). Code identifiers (`pickscore`, `PickScoreBreakdown`, table and route names) stay as they are.

**PickScore page (Jack approved 2026-10-26).** Answers: are the scores right and fresh, which laptops have problems and why? Same route as before: data survey → design → build
1. Status: last recalculated, amber if the catalog changed since; Recalculate (confirmed) with the job panel
2. Problem laptops: withheld, and Unverified (CPU or GPU unresolved) with the catalog string and the closest PassMark rows, so a naming mismatch like 6b is visible without a survey
3. Proxy-scored counts (info): GPU via the integrated-GPU map, via the Apple equivalence
4. Score distribution per use case (bar chart)
5. Top 10 per use case, to sanity-check the ranking
6. Weights per use case (read-only)
- The group "Scoring & Search" is renamed "Scoring": PickScore, CPU Benchmarks, GPU Benchmarks

**Admin look, round 2 (Jack approved 2026-10-26):**
- Chart palette, light mode, darker steps so every colour passes 3:1 on white: #2a78d6, #d9622b, #0e8aa8, #b07800, #cf5a8a, #2f8a2f, #6250d6; dark mode keeps its own steps (#3987e5, #d95926, #1a9fbf, #c98500, #d55181, #008300, #9085e9), only the third slot changed (Jack replaced the aqua #16966a with cyan, 2026-10-26; both modes re-validated). With that, "View as table" is removed from every chart; tooltips and the brand bars' value labels stay
- Line charts use straight segments between daily points, never smoothed curves (a curve draws values that don't exist, e.g. a rise before a spike day)
- The island sidebar becomes frosted glass (semi-transparent, backdrop blur) over a page background with a few large, faint blurred circles (blue top-left, cyan lower-left, violet bottom-right); fainter in dark mode; static, no animation; falls back to plain white where backdrop blur isn't supported
- Admin content fills the available width beside the island at every screen size; no max-width cap (Jack, 2026-10-26: a 1600px cap left empty space on wide screens)
- Monitoring group designs (Chatbot Monitoring, Token Usage, LLM Settings, Jobs) are in `docs/admin-redesign/`; build them after a data survey, as with the Dashboard

**Admin tidy-up batch (Jack approved 2026-10-26, all of it), one prompt after the PickScore page:**
- Stale copy (e.g. AI Clean-Up says there is no cancel, but Jobs has Stop)
- Processor model only in LLM Settings (remove it from AI Clean-Up)
- Sources page: show and edit evidence tier, market relevance, review language (not only the deprecated `trust_tier`)
- Jobs page shows each job's result, not only errors
- Laptop edit form can edit tags and family
- Raw Records: a "process this one" action
- Scrape Queue search covers all rows, not only the current page
- Confirmations for actions that change the live site (Regroup, inline status change, releasing laptops from a family, Rematch, Remove a review link, Questionnaire reorder, Scrape this brand)
- One type scale on the remaining pages

### Benchmark name matching fix (step 6b)

Survey 2026-10-25 (read-only, production): 32 of 333 active laptops have an unresolved CPU or GPU; only the HP OmniBook 3 16-bz0037QU is withheld (both). Every part is already in the benchmark table, so a refresh fixes none of them.
- **A. Snapdragon part numbers (13 laptops, CPU):** the catalog writes `X1-26-100` / `X1 26 100`, PassMark writes `X126100` / `X1E-78-100`. Fuzzy score for the right row is 0.750–0.855, below the 0.90 threshold. Lowering the threshold is rejected: at that level wrong rows win (Cobalt 100, a 2005 Athlon 64 X2). Fix by normalising the part number, targeted, not a lower threshold
- **B. Integrated GPUs (20 laptops, all HP, GPU):** names like "AMD Radeon Graphics" / "Intel Graphics" go through `_INTEGRATED_GPU_BY_CPU`, which has no key for 10 HP CPUs. Fix by adding those entries, each with a source (Intel ARK, AMD product page), like the existing ones. The OmniBook 3 also misses because the map key uses spaces and HP uses hyphens, so the same normalisation applies to map keys
- Scores for these 32 change on purpose. Other laptops may move slightly because factors are percentiles; measure it
- Applied 2026-10-25 (option B, Jack approved): Qualcomm part numbers joined on all sides, plus an exact part-number match before fuzzy matching (an identity match, so it doesn't consult the 0.90 threshold; same idea as the `_laptop_variant` GPU rewrite). X1E-78-100 → PassMark id 6095 (22,695), the curated row. 9 new `_INTEGRATED_GPU_BY_CPU` entries with Intel ARK / AMD sources. Read-only recompute: 0 withheld (OmniBook 3 16-bz0037QU now 46 general), most of the 32 go down (the fabricated 50 was flattering weak parts), 202 others move by at most 1 point (percentile rounding), no previously resolved part changes row
- Known gap: PassMark has one generic "Intel Graphics" row for the Panther Lake iGPUs (386H, 355, 325, 322, 356H), so these are likely under-scored, the 4-Xe-core 356H most of all

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

**Background jobs (added 2026-10-25)**
- Every long admin process runs as a background job instead of a request that waits: scrape, process (Gemini), embeddings, PickScore `generate-all`, review `process-bulk`, transcripts, and the benchmark refresh once it exists
- Each job is recorded in a table: what it is, who or what started it (admin button or schedule), start and end time, status (queued, running, succeeded, failed), progress, result summary and error. The admin shows this list, so a run can be checked without reading Render logs
- Why: today these are plain requests (curl or admin buttons). Long ones time out, show no progress, and a failure is only visible in the logs
- Where a job runs: short jobs (e.g. `generate-all`, minutes) can run on Render. Long ones (scraping every site, multi-day review runs) run in GitHub Actions, because Render's free tier kills running work on every deploy and when the service sleeps. Both write to the same job table
- A job killed mid-run must show as failed or interrupted, never stay "running" forever
- Survey first: how each process is started today, how long it takes, and whether the admin already has a job or result table to build on
- Failed jobs feed the notifications below
- PickScore `generate-all` moved to a background job early (2026-10-25), before step 6 and 6b deploy, so Jack can follow the run in /admin/jobs

**Notifications (new feature)**
- Email through Brevo (already set up for auth emails) + in-app notifications in the admin
- Recipient: Jack (admin) only for now. Design the table so user notifications can be added later without a rewrite
- Events: scheduled run failed; a site returned no data (likely a redesign or block); a price changed by more than ~20%; new laptops that went live; new laptops held back and why; laptops added without a price (not in any guide), listing each one so Jack can fill them in; run summary; a configured LLM returned 404 (model retired or renamed by Google; a 404 does not trigger fallback, so that feature stays broken until the setting is changed)

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

- Laptop categories as main labels on the details page (decided: redesign, then re-tag, then show). Survey 2026-10-22: tags are LLM-assigned at catalog processing (1–3 per laptop, `engine.py` rule 11), only 4 values in use, and Business is on 245 of 333 active laptops (including a 4GB Chromebook), so it distinguishes almost nothing. Gaming is accurate (85 of 89 have an RTX/RX GPU; the other 4 are Ryzen AI Max). The `categories` table also holds Apple customization groups (storage, memory, apple_care…) that must be filtered out. Plan: redesign the tag set (e.g. along the manufacturer segments compared earlier: Student, Everyday/Home, Business limited to real business lines, Creator, Gaming, Workstation, AI PC), re-tag every laptop, then show the tags on the details page. Still to decide: the exact tag set, and where this goes in the order
- Brand and screen-size weight with no preference: in "For you" mode a wizard user who gave no brand preference still has brand at 24% weight, every laptop scoring a neutral 50, so about a quarter of the score carries no information (seen 2026-10-25). Same issue in general mode (~7 points constant). Decide whether the weight is dropped and redistributed when no preference is given
- "Best fit" should only pick from the use cases, never "For you": the HP OmniBook 3 16-bz0037QU showed "Best fit: For you · 49" while its stored use-case scores were still withheld (2026-10-25)
- MacBooks get their highest PickScore in gaming (e.g. MacBook Pro 16 M5 Max: gaming 77, creative 76) because the Apple GPU equivalence gives a high GPU score, while macOS runs few games. Partly fixed in step 6 (2026-10-25): "Best fit" skips Gaming for macOS, and the Gaming tab notes the score reflects hardware only; stored scores unchanged. Still open: whether the gaming ranking (step 8) leaves macOS out (today they sort after measured GPUs, MacBook Pro M5 Max at #41 of 89 lines). Affects Pico's details-page opening ("which use cases it suits") and the scores shown on ranking pages. Related to game compatibility (deferred in 2.1)
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
