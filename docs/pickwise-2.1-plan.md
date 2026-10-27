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
- No site needs a browser. Apple, HP, ASUS, ROG and PassMark all work with Scrapling's plain `Fetcher`; Acer stays on manual HTML upload (Akamai refuses both `Fetcher` and `StealthyFetcher`). Once every scraper is switched, Playwright, patchright and Chromium come out of the image (about 715 MB of browser plus the `--with-deps` apt libraries), and the project depends on plain `scrapling` + `curl_cffi`, not the `[fetchers]` extra (which also needs playwright ≥1.62, against our 1.60 pin)
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
