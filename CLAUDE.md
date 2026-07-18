# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Database**: `DATABASE_URL` points at a hosted Supabase Postgres — no local DB needs to be started. `docker compose up -d` only exists as a local-Postgres alternative if you deliberately point `DATABASE_URL` at localhost.

**Run the development server:**
```bash
# From project root, with venv activated
uvicorn app.main:app --reload
```

**Activate virtual environment (Windows):**
```bash
venv\Scripts\activate
```

**Database migrations:**
```bash
# Apply all pending migrations
alembic upgrade head

# Create a new migration (auto-detect model changes)
alembic revision --autogenerate -m "description"

# Rollback one migration
alembic downgrade -1
```

**Install Playwright browsers (first-time setup):**
```bash
playwright install chromium
```

**Run the agent eval harness (from project root — `.env` resolves against CWD):**
```bash
python eval/run_eval.py run --label baseline                 # all 30 queries, with LLM judge
python eval/run_eval.py run --label quick --no-judge         # rule checks only
python eval/run_eval.py run --label x --only relevance_gating  # one category
python eval/run_eval.py run --label x --ids relax_zh_003,clear_en_002  # specific cases
python eval/run_eval.py compare eval/runs/A.jsonl eval/runs/B.jsonl
```

## Environment Variables

Create a `.env` file in the project root. Required variables (see `app/config.py`):

```
DATABASE_URL=postgresql://postgres:password@localhost:5432/pickwise_v2
SECRET_KEY=your-secret-key
SMTP_USERNAME=your-gmail@gmail.com
SMTP_PASSWORD=your-gmail-app-password
GEMINI_API_KEY=your-gemini-api-key
YOUTUBE_API_KEY=your-youtube-data-api-key   # optional — server starts without it
SERP_API_KEY=your-serpapi-key               # optional — market price tool's live-listings layer (SerpApi Google Shopping); catalog layer works without it
GOOGLE_OAUTH_CLIENT_ID=xxx.apps.googleusercontent.com  # optional — Sign in with Google; POST /auth/google returns 400 without it
WEBSHARE_PROXY_USERNAME=xxx                 # optional — Webshare rotating-RESIDENTIAL proxy for review transcript fetches;
WEBSHARE_PROXY_PASSWORD=xxx                 #   direct connection when unset (fine locally; Render's datacenter IP is blocked by YouTube)
```

`GEMINI_API_KEY` is a required setting (`app/config.py`) and is passed explicitly (`google_api_key=settings.gemini_api_key`) to every Gemini client — agent, embeddings, processor, recommendation, review processor, and the eval judge. Do **not** rely on the ambient `GOOGLE_API_KEY` / Application Default Credentials — that won't exist in Docker/production.

`YOUTUBE_API_KEY` is optional (`Optional[str] = None` in `app/config.py`) — the server starts without it; the review discovery/ingest endpoints raise 400 at call time if it's missing.

The Docker Compose DB defaults: user=`postgres`, password=`password`, db=`pickwise_v2`.

## Architecture

### Data Pipeline (core flow)
```
Feed Crawler → laptop_scrape_urls (ScrapeTarget)
     ↓
Scrape URL / Bulk Scrape → raw_scrap_laptops (RawScrapLaptop, status: pending)
     ↓
AI Processor (Gemma gemma-4-31b-it) → laptops (normalized, multi-variant)
     ↓
Embeddings (gemini-embedding-2, 768-dim) → laptop_embeddings
```

Independent of the flow above: Benchmark Scraper (PassMark) → `cpu_benchmarks` / `gpu_benchmarks` (consumed by PickScore), and the YouTube review ingestion pipeline (see `app/reviews/`):

```
discover_videos (YouTube Data API) → fetch_transcript → RapidFuzz match to laptop
     → raw_youtube_reviews (matched/pending/rejected)
     → process (45s chunks → Gemini summary + sentiment → embed) → laptop_review_chunks
     → aggregate → laptop_review_summary
```

### Module Structure

Each domain module under `app/` follows a consistent pattern: `models.py` (SQLModel tables + Pydantic schemas), `router.py` (FastAPI endpoints), and supporting files.

- **`app/users/`** — Auth (username-or-email login, Google Sign-In via ID-token verification), JWT, bcrypt, email verification, preferences (`laptop_user_preference` table, `budget` stored as `{min, max}` JSON range). Avatar gateway: `PUT/DELETE /auth/me/avatar` + public `GET /auth/avatar/{user_id}` — bytes in the separate `user_avatars` bytea table (Render fs is ephemeral; separate table so `get_current_user` never loads the blob), magic-byte validation, 2 MB cap. Google login imports the `picture` claim into the same table on create/link only (best-effort, never on returning logins — a deleted avatar must stay deleted). `questionnaire_model.py`/`questionnaire_router.py` expose the 6-step preference survey as a dynamic catalog (`GET /questionnaire?product_type=laptop`, `include_inactive=true` for admin views) plus admin-only CRUD (create/update/delete; 409 if another active question occupies the same `step_order` for the product type). Answers are still written via the existing `PUT /me/preferences`. `question_type` is a native Postgres enum (`questiontype`) — adding a Python enum member requires an `ALTER TYPE ... ADD VALUE` migration (e.g. `MULTIPLE_CHOICE` in `c8f24d1e9a37`).
- **`app/laptops/`** — 9-part laptop spec model, brands (UUID FK), customizations (bulk/pattern creation, `category_id` FK into `app/taxonomy/`), price history, hybrid vector search. `laptop_category_model.py` owns the `laptop_categories` many-to-many junction (laptops ↔ tags).
- **`app/taxonomy/`** — `product_type_model.py`/`product_type_router.py` (small stable set, e.g. `"laptop"`, scopes the questionnaire) and `category_model.py`/`category_router.py` (marketing/use-case tags for the frontend tag component) — both mirror `app/laptops/brand_model.py`'s CRUD shape exactly (admin-only writes, public reads, 409 on duplicate/still-referenced).
- **`app/pickscore/`** — Product-agnostic scoring engine (see PickScore section below)
- **`app/laptops/pickscore_adapter.py`** — Converts `Laptop` → `ScorableProduct`; owns laptop range DB queries (calibrated from catalog laptops, not global benchmark table)
- **`app/laptops/pickscore_router.py`** — `/laptops/calculate-score` endpoints
- **`app/embeddings/`** — `service.py` builds a natural-language document per laptop and embeds it via Gemini `models/gemini-embedding-2` (`output_dimensionality=768` pins the model's larger default down to match the `Vector(768)` column without a schema migration). `embed_text()` is the single embedding entry point for the whole app (hybrid search, RAG retrieval, review chunks, recommendation). Changing the embedding model changes the vector space — always re-run `POST /embeddings/generate-all` (and re-process any review chunks) afterwards. `router.py` exposes admin-only generate-all / generate-single / status endpoints
- **`app/recommendation/`** — `service.py` orchestrates the full recommendation pipeline (hybrid search → batch PickScore → Gemini LLM); `router.py` exposes `POST /recommendations/laptops` (auth required); `schemas.py` owns request/response models including internal `_LLMOutput` for structured Gemini output
- **`app/rag/`** (renamed from `app/conversations/`) — RAG pipeline + conversation-thread persistence, now consumed as a library by the agent (see `app/agent/` below) rather than driven by its own chat endpoint. `retrieval.py` (Module 1), `reranker.py` (Module 2), `relaxation.py` (Module 3), `gating.py` (Module 4), `evaluation.py` (Module 5 + live logging, still writes `pipeline_eval_logs` on every `search_laptops` tool call). `service.py` is conversation-thread CRUD only (`create_conversation`, `list_conversations`, `get_conversation`, `delete_conversation`). `router.py` exposes 4 `/conversations/` endpoints (all auth-required, URL prefix unchanged) — create/list/get/delete a thread; **no `/chat` route** (removed, superseded by `POST /agent/chat`). `models.py` owns `Conversation`, `Message`, `ConversationLaptop`, `PipelineEvalLog` tables.
- **`app/agent/`** — ReAct agent built with `langchain.agents.create_agent` (migrated off the deprecated `langgraph.prebuilt.create_react_agent`), the sole conversational entry point (`POST /agent/chat`). `graph.py` owns `AGENT_MODEL = "gemma-4-31b-it"` / `AGENT_TEMPERATURE = 0.3` and the `build_agent_llm()` factory — the single source of truth for the agent LLM config; `eval/run_eval.py` imports `build_agent_llm` (and `_SYSTEM_PROMPT`) so offline evals always measure the production setup. The system prompt contains two enforcement blocks added after eval failures: **SCOPE ENFORCEMENT** (refuse all non-laptop tasks — fixed the agent doing unrelated tasks like letter-writing when asked in Chinese) and **FACTUAL GROUNDING** (never quote prices from memory — call `search_malaysian_market_price` for real numbers; work with returned `search_laptops` results before suggesting anything from memory; treat `0.0`/"unrecognized" tool values as unavailable, never borrow numbers from a different configuration). A third prompt block tells the agent to cite each result's `pick_score` as "PickScore N/100" with its top factors, and never to invent a score for unscored results. `tools/search_laptops.py` absorbs the CRS retrieve→rerank→relax→gate pipeline as a tool; after gating it batch-computes **general-mode PickScore** on the top-k results (same ranges+benchmarks-once pattern as `app/recommendation/service.py`; no user context in tools, so never personalized) and attaches `pick_score` + a compact `pick_score_top_factors` summary to each result — full 8-factor breakdowns are deliberately omitted to keep tool output within the eval judge's truncation budget. PickScore failure is non-fatal (results go out unscored); `tools/laptop_tools.py` owns `calculate_custom_apple_price` and `get_review_evidence`; `tools/market_price.py` owns `search_malaysian_market_price` — two-layer price lookup: (1) catalog layer from own DB (official `price_rm` + last 5 `laptop_price_history` snapshots; `model_code` exact match first, else RapidFuzz `token_set_ratio ≥ 75` on `product_name`, fuzzy hits carry a "closest match — verify chip generation/size" note), and (2) live-listings layer via SerpApi (serpapi.com) Google Shopping geo-targeted to Malaysia (`SERP_API_KEY`, optional; free tier ~100 searches/month — replaced Serper.dev, and before that the parse.bot iPrice integration, which lacked laptop coverage). Live-listings hygiene: RapidFuzz title-relevance ≥ 60 **plus** an accessory-keyword blocklist and a RM 800 price floor (token_set_ratio scores 100 for "<laptop name> skin/battery/…" titles, so relevance alone is not enough), max 2 listings per store so the price range reflects the market, 6-hour in-process cache on successful lookups only. Shopee/Lazada search links are always included as the last-resort fallback. `graph.py`'s `run_agent()` reconstructs conversation state from the `messages`/`conversation_laptops` tables each turn (no LangGraph checkpointer) and flattens the reply through `_content_to_text()` — Gemini can return content as a list of typed blocks (`thinking` + `text`) instead of a plain string, which crashed the `messages` insert (`psycopg2 can't adapt type 'dict'`); text blocks are joined, thinking blocks dropped (fallback-only). `router.py` accepts an optional `conversation_id` (auto-creates if omitted), persists messages + the laptop shortlist pool (including `pick_score`) after each turn, and returns a structured `laptops` field alongside the text reply for frontend score badges. `POST /agent/chat/stream` is the SSE twin (same request body/auth): events are JSON per `data:` line — `meta` (conversation_id, first), `token` (append to bubble), `thinking` (model reasoning delta — extracted per chunk by `_chunk_to_thinking` and streamed on its own channel so the frontend renders a thinking flow; never persisted, and note it exposes raw reasoning to the client), `turn_reset` (discard current bubble — internal tool-call turn text; thinking is NOT discarded), `tool` (activity indicator), then `done` (conversation_id + the same `laptops` payload) or `error`; persistence happens after the stream completes via the shared `_persist_assistant_turn()`, and `graph.py`'s `stream_agent()` filters thinking blocks out of reply tokens (`_chunk_to_text` — no thinking fallback, unlike `_content_to_text`) — fresh `search_laptops` results when a search ran this turn, otherwise the persisted `conversation_laptops` pool joined to `laptops` (ordered by similarity, NULLS LAST). `AgentLaptopCard` includes `image_url` (first `image_urls` photo) for card thumbnails — looked up from the `Laptop` row in `router.py` when building cards, deliberately NOT added to the search tool's payload so image URLs never enter the LLM context.
- **`app/reviews/`** — YouTube review ingestion pipeline (all endpoints admin-only). `discovery.py` resolves channel URLs (4 formats) and discovers videos via YouTube Data API v3 (`search.list`, 100 quota units/channel, top 5 per channel); `transcript.py` fetches transcripts via `youtube-transcript-api` **v1.x instance API** (`YouTubeTranscriptApi().fetch()`, no quota cost) — routed through a Webshare rotating-residential proxy when `WEBSHARE_PROXY_USERNAME`/`_PASSWORD` are set; without it, YouTube IP-blocks datacenter hosts (Render), every fetch throws, and all discovered videos land as `rejected` (all errors are swallowed to `None` — check the logs for the real cause); `matcher.py` fuzzy-matches video titles to catalog laptops (RapidFuzz `token_set_ratio`, threshold 73, compact match keys that strip `-inch`/RAM/storage and extract the chip from parens; stored `match_confidence` can exceed the threshold on rows scored by an older matcher config — run `/reviews/rematch` after matcher changes); `processor.py` chunks transcripts into 45-second windows → Gemini summary + sentiment tag (`strength`/`weakness`/`neutral`) → embed via the central `embed_text()` from `app/embeddings/service.py` (model `_CHUNK_MODEL = "gemma-4-31b-it"`, same as the agent but kept as a local constant so the review pipeline doesn't import the agent stack; 4s delay between Gemini calls); `aggregator.py` rolls up top-5 strengths/weaknesses into `laptop_review_summary`; `service.py`'s `ingest_for_laptop()` runs discovery → transcript → match end-to-end (retries `rejected` rows, skips `matched`/`pending`); `ingest_bulk()` (`POST /reviews/ingest-bulk?limit=&skip_covered=`) runs it across the catalog one search per laptop *family* (`_family_key` truncates `product_name` at the first paren — config variants share one YouTube search, since discovery costs ~`active_channels × 100` quota units per query against a 10k daily quota; `skip_covered=true` skips families that already have a matched raw review, so daily runs walk the catalog). Chunk processing and aggregation are manual admin steps (`POST /reviews/process/{id}`, `POST /reviews/aggregate/{laptop_id}`); `POST /reviews/process-bulk?limit=` (default 5, max 50) runs processing over every `matched` review that has no `laptop_review_chunks` rows yet — existing chunks are the "already processed" marker, since processing never flips the review's status, which also makes re-runs duplicate-safe; per-review failures are reported in the response without aborting the run; `POST /reviews/rematch` re-runs auto-matching on all pending rows.
- **`app/scraper/`** — Playwright-based crawlers for Apple (DOM) and Asus/ROG (`window.__NUXT__` JSON state)
- **`app/processor/`** — LangChain + Gemini LLM extraction from raw scraped data into structured `Laptop` records. Also does category tagging: the extraction schema's `categories` field has the model pick 1–3 use-case tags per variant, preferring the active `categories` rows injected into the prompt as `[AVAILABLE CATEGORIES]`; unknown tags are auto-created in the `categories` table (case-insensitive match, inactive rows matched too so they're never duplicated) and linked via `laptop_categories`. Linking is additive — re-processing never removes manually-assigned tags. `POST /processor/categorize-untagged?limit=N` (admin) backfills laptops with zero category links: one Gemma call per laptop from its stored specs (via `build_laptop_embedding_text`), 5 s throttle, safe to re-run until `untagged_remaining` is 0.
- **`app/benchmark/`** — PassMark CPU/GPU scraping with PostgreSQL upsert (`ON CONFLICT DO UPDATE`)
- **`app/logger.py`** — Centralized logging service. `setup_logging()` called once in `main.py` (console + 5 MB rotating `logs/app.log`). `get_logger(__name__)` is the drop-in for all modules. `get_eval_logger()` returns the special `pickwise.eval` JSON-lines logger used by `rag/evaluation.py`. File logging is **best-effort**: if `logs/` isn't writable (container with non-root user / read-only fs), both loggers warn and fall back to console/stdout instead of crashing startup — a root-owned `WORKDIR` once took down the whole deploy via `PermissionError` in `mkdir`.
- **`app/config.py`** — `pydantic-settings` loading from `.env`
- **`app/database.py`** — SQLModel engine, `get_session()` dependency, pgvector extension init
- **`eval/`** — offline agent eval harness (not part of the server). `queries.yaml`: 30 bilingual queries (zh/en/mixed-Manglish) across 5 categories — clear_intent, vague_intent, constraint_relaxation, relevance_gating, tool_routing — each with expected tools / forbidden tools / budget cap / judge rubric. `run_eval.py`: runs each query through the agent in-process (same `AGENT_MODEL`/`_SYSTEM_PROMPT` as production, single-turn, no history), applies deterministic rule checks, then an LLM judge (`gemma-4-31b-it`) that grades **only against the raw tool outputs** — never its own product knowledge (the catalog is newer than any model's training data). Results land in `eval/runs/<date>_<label>.jsonl`; `compare` diffs two runs and flags regressions. All agent + judge LLM calls share one global rate limiter (default 7 RPM; `--rpm` to override) — the binding free-tier limit is the **16,000 input-TPM cap**, not the 15 RPM request cap: judge prompts carry up to ~27k chars of tool output, so runs must stay below 8 RPM or high-context tool_routing cases 429 (this contaminated the 2026-07-18 gemma-baseline run with 1 failed + 3 unscored entries). Judge failures after 3 retries mark the entry ⚠️ unscored — runs with unscored entries must not be used for compares. Tool outputs are truncated per-output (4,500 chars each, 27,000 total — sized so a full 5-result `search_laptops` payload with pick_score fields survives intact) — never head-truncate the joined blob, or evidence in later tool calls becomes invisible and grounded responses get falsely judged as fabrication.

### Key Design Decisions

**Circular import resolution**: `laptop_models.py` ↔ `customization_model.py` use `TYPE_CHECKING` guards plus a deferred import at the bottom of `laptop_models.py` to register `LaptopCustomization` in SQLAlchemy's mapper registry without a circular dependency.

**Apple Silicon GPU scoring**: PassMark has no separate GPU benchmark entries for Apple ARM chips — any fuzzy match would be a false positive. `_score_gpu()` in `app/pickscore/engine.py` short-circuits for `brand_name == "apple"` and always returns the CPU score as a proxy, setting `flags.gpu_score_is_proxy = true`. Non-Apple GPUs use normal fuzzy benchmark lookup with a fallback of 50.

**Benchmark range calibration**: `get_laptop_ranges()` in `app/laptops/pickscore_adapter.py` derives CPU/GPU min/max by fuzzy-resolving models that are actually in the catalog (`laptops` table), not from the global PassMark tables. This prevents desktop/server CPU scores (200,000+) from collapsing all laptop scores toward zero. Falls back to the global PassMark table only if no catalog models resolve successfully.

**Recommendation pipeline** (`app/recommendation/service.py`): hybrid vector search → batch PickScore → Gemini `gemini-3.5-flash` with `with_structured_output(_LLMOutput)`. Requires an existing `laptop_user_preference` row (returns 400 otherwise). Default `top_k = 3` (candidates pool = 15). LLM language level is driven by user's `tech_savviness` field.

**CRS pipeline absorbed into the agent** (`app/rag/` + `app/agent/tools/search_laptops.py`): the 5-module pipeline — retrieve (top-50 pgvector) → rerank (`final_score = similarity × penalty + bonus`) → relax (stepwise: weight first, then budget) → gate (calibrated threshold `0.53` — embedding-model-specific, re-derive on any embedding model change; see `app/rag/gating.py`) — is called directly from the `search_laptops` agent tool instead of running as a separate, fixed-order chat pipeline. There is no standalone intent-detection call anymore; the LangGraph agent decides per turn, from full conversation context, whether to call `search_laptops` again or answer from the existing `conversation_laptops` pool. On a gated (low-confidence) result the tool returns no laptops plus a `bottleneck`/`message` pair, and the agent — not the tool — is responsible for turning that into a clarifying question rather than a dead end. Every `search_laptops` call still writes to `pipeline_eval_logs` (DB) and `logs/eval/pipeline_trace.jsonl`. `UserConstraints` is a decoupled dataclass — not tied to `LaptopUserPreference` — built directly from the tool's own args (`budget_max`, `brand`, `purpose`).

**Taxonomy split — `product_types` vs `categories`**: two separate small lookup tables in `app/taxonomy/`, not one merged table — they serve different admin workflows. `product_types` is a small, stable set (product line: laptop today, phone/other 3C later) that scopes `questionnaire_questions`. `categories` is dynamic, frontend-facing marketing/use-case tags (Gaming, Business, Creator, etc.), many-to-many with `laptops` via the `laptop_categories` junction table (added with zero changes to the already-complex 200+ field `laptops` model) and one-to-many with `laptop_customizations` (`category_id`, migrated off a free-typed string that had drift risk).

**`LaptopUserPreference.budget` is a `{min, max}` JSON range, not a single ceiling**: the v1 questionnaire's budget question is range-bucketed ("< RM 2000" … "> RM 5000"), so a single ceiling int couldn't represent the open-ended top bucket without a fabricated cap. `max: null` means no upper limit. `app/pickscore/engine.py::_score_price()` reads `budget["max"]` only — `min` is informational, never a scoring penalty (being under budget was never penalized).

**Windows Playwright compatibility**: All async Playwright calls are offloaded to a dedicated worker thread with its own `ProactorEventLoop` via `app/scraper/playwright_utils.py`. This prevents event loop conflicts with Uvicorn's default `SelectorEventLoop` on Windows.

**Asus scraper dual-path**: ROG pages (`rog.asus.com`) read `window.__NUXT__.state.Spec.spec`; standard ASUS pages (`www.asus.com`) read `window.__NUXT__.state.PDPage`. Both return a `list[dict]` — one entry per SKU variant — with `?v=N` URL suffix for multi-variant pages.

**AI Processor model**: `gemma-4-31b-it` via `ChatGoogleGenerativeAI` (LangChain). Free-tier limits: **15 RPM, 1500 RPD, and a strict 16,000 input-TPM cap** (Google added the TPM cap — it, not RPM, is the binding limit for high-context calls). The processor enforces a 5-second inter-request delay and a default batch of 100 per run (~8 min) to stay safely under 15 RPM; its per-request prompts are small enough that 16k input-TPM is not usually binding there, unlike the eval harness. Hard ceiling is 1500 per run (`?limit=1500`). AFC (Automatic Function Calling) is enabled by the Google SDK — it self-corrects structured output against `ExtractedLaptopFamily` for up to 10 rounds per request.

**AI Processor safety rules**: The Gemini prompt enforces combinatorial safety (base configs first) and price matrix isolation (output `0.0` for upgrade prices not explicitly stated) to prevent hallucinated pricing. Duplicate SKUs are handled via an explicit **upsert**: query by `model_code` first — if exists, update all fields in-place and record price history only if `price_rm` changed and is non-zero; if new, insert and record the initial price snapshot. `model_code` is never overwritten during an update (it is the lookup key).

**Auth scopes**: JWT tokens are scoped — `email_verification` (1hr), `password_reset` (15min), and standard access tokens (7 days default). The `get_current_admin` dependency enforces `role == "admin"` (403 otherwise).

**Hybrid vector search** (`POST /laptops/hybrid-search` in `laptop_router.py`): embeds the user query with the same embedding model used for the documents (via `embed_text()`), then runs a single SQL statement that joins `laptops` ↔ `laptop_embeddings` ↔ `laptop_brands`, orders by pgvector cosine distance (`<=>`, ascending = closest), and applies optional `budget_max`/`brand` hard filters in the same query. Distance is converted to `similarity_score = 1 − distance` in the response (higher = better). Returns a ranked candidate pool (`LaptopSearchResult`) intended to feed the downstream rerank → PickScore → LLM stages.

**Price history**: `Laptop` price is captured into `laptop_price_history` in three places: (1) `POST /laptops/` router on manual create, (2) `PUT /laptops/{id}` router when `price_rm` changes, and (3) the AI processor (`app/processor/engine.py`) on every new insert and on re-process when `price_rm` changes and is non-zero. `GET /laptops/{id}/price-history` returns the ordered series. This prevents vendor price changes from being silently lost and backs the planned Price History Tracker chart.

### Database Tables

All tables use UUID primary keys. Key relationships:
- `laptops` → `laptop_brands` (FK: `brand_id`)
- `laptops` → `laptop_customizations` (1:N)
- `laptops` → `laptop_embeddings` (1:1, 768-dim pgvector)
- `laptops` → `laptop_price_history` (1:N; snapshot on create + on price change)
- `laptops` → `laptop_pick_scores` (1:N; one row per use case, unique on `laptop_id + use_case`; precomputed general-mode PickScores)
- `raw_scrap_laptops` → `laptop_brands` (FK: `brand_id`)
- `laptop_scrape_urls` → `laptop_brands` (FK: `brand_id`); has `scrape_status` (`pending`/`completed`/`failed`/`skipped`) and `is_active` flag
- `laptop_user_preference` → `users` (FK: `user_id`); `budget` is `{min, max}` JSON, not a plain int

**Taxonomy:**
- `laptops` ↔ `categories` (M:N via `laptop_categories` junction: `laptop_id`, `category_id`)
- `laptop_customizations` → `categories` (FK: `category_id`)
- `questionnaire_questions` → `product_types` (FK: `product_type_id`)

**Conversational History & Memory (live):**
- `conversations` → `users` (FK: `user_id`); stores `title` (auto-generated from first message, truncated 60 chars), `created_at`, `updated_at`
- `messages` → `conversations` (FK: `conversation_id`); `role` (user/assistant enum), `content` (Text), `created_at`
- `conversation_laptops` → `conversations` + `laptops`; the agent's current shortlist pool for a thread — replaced whenever `search_laptops` returns a high-confidence result, otherwise left as-is for follow-up context; stores `pick_score` (general-mode, computed at search time) + `similarity_score` snapshots that back the `laptops` field in the `/agent/chat` response
- `pipeline_eval_logs` → `users` + `conversations`; one row per live user request — `gate_status`, `top_score`, `relaxed_field`, `relaxed_from`, `relaxed_to`, `bottleneck`, `candidate_count`, `result_laptop_ids`

**YouTube Reviews:**
- `youtube_channels` — `channel_id`, `channel_name`, `channel_img_url`, `trust_tier` (`tier_1`/`tier_2`), `active`
- `raw_youtube_reviews` → `laptops` (FK: `matched_laptop_id`, nullable); `video_id`, `raw_transcript` (JSONB), `match_confidence`, `status` (`pending`/`matched`/`rejected`)
- `laptop_review_chunks` → `laptops` + `raw_youtube_reviews`; `chunk_text` (LLM summary), `embedding` (768-dim pgvector), `sentiment_tag`, `timestamp_start/end_seconds` (used for YouTube timestamp links in `get_review_evidence`)
- `laptop_review_summary` → `laptops` (1:1); `aggregated_strengths`/`aggregated_weaknesses` (JSONB top-5 each), `review_count`

### API Access Control

All routes are served under `/api/v2` (added via `prefix="/api/v2"` on every `app.include_router(...)` call in `app/main.py`, not on the individual routers themselves — e.g. `/laptops` in this doc means `GET /api/v2/laptops`). `GET /`, `/docs`, `/redoc`, `/openapi.json` are unprefixed. The OAuth2 password-flow `tokenUrl` in `app/users/auth.py` is `"api/v2/auth/login"` to match, for Swagger UI's Authorize button.

- Public endpoints: GET laptops (incl. `POST /laptops/hybrid-search`, `GET /laptops/{id}/price-history`, `GET /laptops/{id}/pick-scores`, `GET /laptops/pick-scores/ranking`), GET brands, GET benchmarks, GET product-types, GET categories, GET questionnaire, auth registration/login/verify
- Bearer token required: `/auth/me/*` profile and preferences endpoints; `POST /laptops/calculate-score` and `/calculate-score/batch`; `POST /recommendations/laptops` (also requires an existing `laptop_user_preference` row — 400 if missing); `/conversations/` endpoints (create/list/get/delete a conversation thread only — no preference-row requirement); `POST /agent/chat` (no preference-row requirement — `search_laptops` args are passed directly by the LLM)
- Admin only (`role == "admin"`): all write operations for laptops, brands, customizations, scraper, processor, benchmarks, embeddings, product-types, categories; raw scrap data listing; all `/reviews/*` endpoints (channels, ingest, raw listing, manual match, rematch, process, aggregate)

### PickScore Engine (`app/pickscore/`)

Fully deterministic scoring engine — no LLM involvement, no product-specific imports. Produces a structured breakdown consumed by the LLM for conversational explanations. Designed to be reused across any 3C product category (laptops, phones, tablets) via an adapter pattern.

**Files:**
- `engine.py` — 8 factor scoring functions + 3-layer weighting pipeline + `calculate_pick_score(product: ScorableProduct, ...)`
- `schemas.py` — `ScorableProduct` dataclass (product-agnostic input contract), `PickScoreResponse`, `BatchPickScoreResponse`, `FactorBreakdown`
- `benchmark_service.py` — RapidFuzz fuzzy matching against benchmark tuples; module-level 5-min cache keyed by normalized model string; confidence threshold 0.6
- `ranges_cache.py` — Generic TTL cache (`get_cached_ranges` / `set_cached_ranges`); no DB queries — callers own the query logic

**Laptop adapter (`app/laptops/`):**
- `pickscore_adapter.py` — `laptop_to_scorable(laptop, brand_name) → ScorableProduct`; `get_laptop_ranges(session) → dict` (queries `laptops`, `cpu_benchmarks`, `gpu_benchmarks`; uses the generic ranges cache with key `"laptop_ranges"`)
- `pickscore_router.py` — `POST /laptops/calculate-score` and `POST /laptops/calculate-score/batch`; owns `LaptopPickScoreRequest` and `BatchLaptopPickScoreRequest` schemas

**Adding a new product category (e.g. phones):**
1. Create `app/phones/pickscore_adapter.py` — implement `phone_to_scorable()` + `get_phone_ranges()`
2. Create `app/phones/pickscore_router.py` — `POST /phones/calculate-score` using the same `calculate_pick_score()` from `app/pickscore/engine.py`
3. Register router in `app/main.py`

**Two modes:**
- **Personalized** — triggered when `user_id` is provided and `laptop_user_preference` exists; uses the user's `priorities` dict (factor → weight 1–10), `purpose` list, `portability` intensity, `budget`, `screen_size`, and `brand_preferences`
- **General** — triggered when `user_id` is null; uses `DEFAULT_PRIORITY` (N-i rule, 8 factors), neutral modifiers, inverse min-max for price, fixed 50 for brand/screen size. `calculate_pick_score(..., priority_override=...)` lets general-mode callers swap `DEFAULT_PRIORITY` for a custom base-weight profile (ignored in personalized mode — a real user's priorities always win).

**Precomputed use-case scores (`app/laptops/pickscore_general.py`)**: `laptop_pick_scores` table stores one general-mode score per laptop × use case (unique on `laptop_id + use_case`), with the full factor `breakdown` + `flags` as JSON. Five use-case weight profiles in `USE_CASE_PRIORITIES` (slugs: `office_study`, `programming`, `gaming`, `creative_work`, `general_use` — the frontend's use-case cards; `general_use` is an explicit price-first all-rounder profile, NOT `DEFAULT_PRIORITY` — the N-i rule gave GPU ~17% weight for daily use). Scores are deterministic — regenerate via admin `POST /laptops/pick-scores/generate-all` after processor imports, benchmark refreshes, or profile changes. Public reads: `GET /laptops/{id}/pick-scores[?use_case=]` and `GET /laptops/pick-scores/ranking?use_case=&limit=`. Ranking caveat: for `gaming` only, rows flagged `gpu_score_is_proxy` (Apple — GPU scored via CPU proxy) sort after all real-benchmark laptops, because a proxied GPU score says nothing about gaming performance.

**8 scoring factors (all output 0–100):**

| Factor | Key logic |
|---|---|
| `price` | Personalized: 100 if ≤ budget, else `max(0, 100 − 2 × overRatio × 100)`. General: inverse min-max |
| `cpu` | `normalize(cpu_mark)` via benchmark service |
| `gpu` | `normalize(gpu_mark)`; Apple always proxied via CPU score (ARM — no PassMark GPU data) → sets `flags.gpu_score_is_proxy=true`; other brands fall back to 50 if no match |
| `ram_storage` | `ramScore×0.6 + storageScore×0.4`; HDD storage gets `−15` penalty |
| `portability` | Inverse normalize on `weight_kg` |
| `battery` | Normalize on `battery_wh` |
| `screen_size` | Bucket distance (≤14"=0, ≤16"=1, 17+"=2); `score = max(0, 100 − distance×40)` |
| `brand` | 100 if brand in `brand_preferences`, else 50 |

**3-layer weighting pipeline:**
```
finalWeight[f] = baseWeight[f] × purposeModifier[f] × portabilityModifier (portability only)
normalizedWeight[f] = finalWeight[f] / sum(finalWeight)
```
Purpose modifiers (capped at ×1.3): Gaming→GPU×1.3/CPU×1.1, Creative→GPU×1.3/RAM×1.2, Programming→CPU×1.2/RAM×1.2, Office→CPU×1.1. Portability multipliers: Yes→×1.4, Neutral→×1.0, No→×0.5.

**Batch endpoint** (`/laptops/calculate-score/batch`) fetches ranges and benchmark data once, then scores all laptops in the pool reusing the same cached data.

**Standalone script caveat**: importing `LaptopUserPreference` in isolation triggers SQLAlchemy mapper resolution that requires `LaptopCustomization` to be registered. Always import `from app.laptops.customization_model import LaptopCustomization` first in test scripts (the server avoids this via `main.py` importing all routers).

### Scraping Status Flow

**`ScrapeTarget.scrape_status`** (`laptop_scrape_urls` table): `pending` → `completed` / `failed` / `skipped`
- Set alongside `last_scraped_at` in `_update_target()` in `bulk_scraper.py`
- `completed` — at least one variant saved to `raw_scrap_laptops`
- `failed` — scraper raised an exception OR zero variants saved; **automatically retried on the next bulk run** (WHERE clause picks up `scrape_status = 'failed'` in addition to `last_scraped_at IS NULL`)
- `skipped` — URL already had entries in `raw_scrap_laptops`; stamped but not re-scraped

**`RawScrapLaptop.processing_status`** (`raw_scrap_laptops` table): `pending` → `processing` → `completed` / `failed`

Bulk scrape queries `is_active=True` AND (`last_scraped_at IS NULL` OR `scrape_status = 'failed'`). Returns HTTP 207 on partial failures; writes timestamped failure logs to `logs/scraper/`.

### Deployment

`.github/workflows/deploy.yml` on push to `main` (doc-only changes excluded via `paths-ignore` — note the key is `paths-ignore`, **not** `path-ignore`, which GitHub silently ignores):

1. **build-and-push** — builds the Dockerfile and pushes to GHCR (`:latest` + `:sha`). `docker/setup-buildx-action` is required before the build step: the `type=gha` layer cache only works with the docker-container buildx driver, and the build fails without it.
2. **deploy** — hits the Render deploy hook (`secrets.RENDER_DEPLOY_HOOK`); Render pulls the image and restarts.

Dockerfile runs as non-root `appuser`; `WORKDIR /app` itself stays root-owned, so any runtime-writable directory must be created and chowned explicitly before `USER appuser` (currently `/app/logs`). The container startup command runs `alembic upgrade head` before uvicorn, and binds `${PORT:-8000}` (Render injects `PORT`). `docker-compose.prod.yml` is a VPS alternative that pulls the GHCR image; its `./logs:/app/logs` bind mount requires the host dir to exist and be writable by UID 1000, or file logging falls back to console.
