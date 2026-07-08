# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Start the database (required before running the server):**
```bash
docker compose up -d
```

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

## Environment Variables

Create a `.env` file in the project root. Required variables (see `app/config.py`):

```
DATABASE_URL=postgresql://postgres:password@localhost:5432/pickwise_v2
SECRET_KEY=your-secret-key
SMTP_USERNAME=your-gmail@gmail.com
SMTP_PASSWORD=your-gmail-app-password
GEMINI_API_KEY=your-gemini-api-key
YOUTUBE_API_KEY=your-youtube-data-api-key   # optional — server starts without it
PARSEBOT_API_KEY=your-parse-bot-api-key     # optional — market price tool falls back to search links without it
```

`GEMINI_API_KEY` is a required setting (`app/config.py`) and is passed explicitly (`google_api_key=settings.gemini_api_key`) to every Gemini client — embeddings, processor, recommendation, agent, and review processor. Do **not** rely on the ambient `GOOGLE_API_KEY` / Application Default Credentials — that won't exist in Docker/production.

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
Embeddings (gemini-embedding-001, 768-dim) → laptop_embeddings
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

- **`app/users/`** — Auth, JWT, bcrypt, email verification, preferences (`laptop_user_preference` table, `budget` stored as `{min, max}` JSON range). `questionnaire_model.py`/`questionnaire_router.py` expose the 6-step preference survey as a dynamic catalog (`GET /questionnaire?product_type=laptop`) — catalog only, answers are still written via the existing `PUT /me/preferences`.
- **`app/laptops/`** — 9-part laptop spec model, brands (UUID FK), customizations (bulk/pattern creation, `category_id` FK into `app/taxonomy/`), price history, hybrid vector search. `laptop_category_model.py` owns the `laptop_categories` many-to-many junction (laptops ↔ tags).
- **`app/taxonomy/`** — `product_type_model.py`/`product_type_router.py` (small stable set, e.g. `"laptop"`, scopes the questionnaire) and `category_model.py`/`category_router.py` (marketing/use-case tags for the frontend tag component) — both mirror `app/laptops/brand_model.py`'s CRUD shape exactly (admin-only writes, public reads, 409 on duplicate/still-referenced).
- **`app/pickscore/`** — Product-agnostic scoring engine (see PickScore section below)
- **`app/laptops/pickscore_adapter.py`** — Converts `Laptop` → `ScorableProduct`; owns laptop range DB queries (calibrated from catalog laptops, not global benchmark table)
- **`app/laptops/pickscore_router.py`** — `/laptops/calculate-score` endpoints
- **`app/embeddings/`** — `service.py` builds a natural-language document per laptop and embeds it via `gemini-embedding-001` (`output_dimensionality=768` to match the `Vector(768)` column); `router.py` exposes admin-only generate-all / generate-single / status endpoints
- **`app/recommendation/`** — `service.py` orchestrates the full recommendation pipeline (hybrid search → batch PickScore → Gemini LLM); `router.py` exposes `POST /recommendations/laptops` (auth required); `schemas.py` owns request/response models including internal `_LLMOutput` for structured Gemini output
- **`app/rag/`** (renamed from `app/conversations/`) — RAG pipeline + conversation-thread persistence, now consumed as a library by the agent (see `app/agent/` below) rather than driven by its own chat endpoint. `retrieval.py` (Module 1), `reranker.py` (Module 2), `relaxation.py` (Module 3), `gating.py` (Module 4), `evaluation.py` (Module 5 + live logging, still writes `pipeline_eval_logs` on every `search_laptops` tool call). `service.py` is conversation-thread CRUD only (`create_conversation`, `list_conversations`, `get_conversation`, `delete_conversation`). `router.py` exposes 4 `/conversations/` endpoints (all auth-required, URL prefix unchanged) — create/list/get/delete a thread; **no `/chat` route** (removed, superseded by `POST /agent/chat`). `models.py` owns `Conversation`, `Message`, `ConversationLaptop`, `PipelineEvalLog` tables.
- **`app/agent/`** — LangGraph ReAct agent, the sole conversational entry point (`POST /agent/chat`). `tools/search_laptops.py` absorbs the CRS retrieve→rerank→relax→gate pipeline as a tool; `tools/laptop_tools.py` owns `calculate_custom_apple_price` and `get_review_evidence`; `tools/market_price.py` owns `search_malaysian_market_price` — live price lookup via the parse.bot iPrice Malaysia aggregator (`PARSEBOT_API_KEY`, optional; free tier 100 credits/month + 5 req/min, hence a 6-hour in-process cache and RapidFuzz title-relevance filtering; falls back to Shopee/Lazada search links when the key is missing or the API errors). `graph.py`'s `run_agent()` reconstructs conversation state from the `messages`/`conversation_laptops` tables each turn (no LangGraph checkpointer). `router.py` accepts an optional `conversation_id` (auto-creates if omitted) and persists messages + the laptop shortlist pool after each turn.
- **`app/reviews/`** — YouTube review ingestion pipeline (all endpoints admin-only). `discovery.py` resolves channel URLs (4 formats) and discovers videos via YouTube Data API v3 (`search.list`, 100 quota units/channel, top 5 per channel); `transcript.py` fetches transcripts via `youtube-transcript-api` **v1.x instance API** (`YouTubeTranscriptApi().fetch()`, no quota cost); `matcher.py` fuzzy-matches video titles to catalog laptops (RapidFuzz `token_set_ratio`, threshold 73, compact match keys that strip `-inch`/RAM/storage and extract the chip from parens); `processor.py` chunks transcripts into 45-second windows → Gemini summary + sentiment tag (`strength`/`weakness`/`neutral`) → `gemini-embedding-001` embed (4s delay between Gemini calls); `aggregator.py` rolls up top-5 strengths/weaknesses into `laptop_review_summary`; `service.py`'s `ingest_for_laptop()` runs discovery → transcript → match end-to-end (retries `rejected` rows, skips `matched`/`pending`). Chunk processing and aggregation are manual admin steps (`POST /reviews/process/{id}`, `POST /reviews/aggregate/{laptop_id}`); `POST /reviews/rematch` re-runs auto-matching on all pending rows.
- **`app/scraper/`** — Playwright-based crawlers for Apple (DOM) and Asus/ROG (`window.__NUXT__` JSON state)
- **`app/processor/`** — LangChain + Gemini LLM extraction from raw scraped data into structured `Laptop` records
- **`app/benchmark/`** — PassMark CPU/GPU scraping with PostgreSQL upsert (`ON CONFLICT DO UPDATE`)
- **`app/logger.py`** — Centralized logging service. `setup_logging()` called once in `main.py` (console + 5 MB rotating `logs/app.log`). `get_logger(__name__)` is the drop-in for all modules. `get_eval_logger()` returns the special `pickwise.eval` JSON-lines logger used by `rag/evaluation.py`.
- **`app/config.py`** — `pydantic-settings` loading from `.env`
- **`app/database.py`** — SQLModel engine, `get_session()` dependency, pgvector extension init

### Key Design Decisions

**Circular import resolution**: `laptop_models.py` ↔ `customization_model.py` use `TYPE_CHECKING` guards plus a deferred import at the bottom of `laptop_models.py` to register `LaptopCustomization` in SQLAlchemy's mapper registry without a circular dependency.

**Apple Silicon GPU scoring**: PassMark has no separate GPU benchmark entries for Apple ARM chips — any fuzzy match would be a false positive. `_score_gpu()` in `app/pickscore/engine.py` short-circuits for `brand_name == "apple"` and always returns the CPU score as a proxy, setting `flags.gpu_score_is_proxy = true`. Non-Apple GPUs use normal fuzzy benchmark lookup with a fallback of 50.

**Benchmark range calibration**: `get_laptop_ranges()` in `app/laptops/pickscore_adapter.py` derives CPU/GPU min/max by fuzzy-resolving models that are actually in the catalog (`laptops` table), not from the global PassMark tables. This prevents desktop/server CPU scores (200,000+) from collapsing all laptop scores toward zero. Falls back to the global PassMark table only if no catalog models resolve successfully.

**Recommendation pipeline** (`app/recommendation/service.py`): hybrid vector search → batch PickScore → Gemini `gemini-3.5-flash` with `with_structured_output(_LLMOutput)`. Requires an existing `laptop_user_preference` row (returns 400 otherwise). Default `top_k = 3` (candidates pool = 15). LLM language level is driven by user's `tech_savviness` field.

**CRS pipeline absorbed into the agent** (`app/rag/` + `app/agent/tools/search_laptops.py`): the 5-module pipeline — retrieve (top-50 pgvector) → rerank (`final_score = similarity × penalty + bonus`) → relax (stepwise: weight first, then budget) → gate (calibrated threshold `0.40`, bottleneck detection) — is called directly from the `search_laptops` agent tool instead of running as a separate, fixed-order chat pipeline. There is no standalone intent-detection call anymore; the LangGraph agent decides per turn, from full conversation context, whether to call `search_laptops` again or answer from the existing `conversation_laptops` pool. On a gated (low-confidence) result the tool returns no laptops plus a `bottleneck`/`message` pair, and the agent — not the tool — is responsible for turning that into a clarifying question rather than a dead end. Every `search_laptops` call still writes to `pipeline_eval_logs` (DB) and `logs/eval/pipeline_trace.jsonl`. `UserConstraints` is a decoupled dataclass — not tied to `LaptopUserPreference` — built directly from the tool's own args (`budget_max`, `brand`, `purpose`).

**Taxonomy split — `product_types` vs `categories`**: two separate small lookup tables in `app/taxonomy/`, not one merged table — they serve different admin workflows. `product_types` is a small, stable set (product line: laptop today, phone/other 3C later) that scopes `questionnaire_questions`. `categories` is dynamic, frontend-facing marketing/use-case tags (Gaming, Business, Creator, etc.), many-to-many with `laptops` via the `laptop_categories` junction table (added with zero changes to the already-complex 200+ field `laptops` model) and one-to-many with `laptop_customizations` (`category_id`, migrated off a free-typed string that had drift risk).

**`LaptopUserPreference.budget` is a `{min, max}` JSON range, not a single ceiling**: the v1 questionnaire's budget question is range-bucketed ("< RM 2000" … "> RM 5000"), so a single ceiling int couldn't represent the open-ended top bucket without a fabricated cap. `max: null` means no upper limit. `app/pickscore/engine.py::_score_price()` reads `budget["max"]` only — `min` is informational, never a scoring penalty (being under budget was never penalized).

**Windows Playwright compatibility**: All async Playwright calls are offloaded to a dedicated worker thread with its own `ProactorEventLoop` via `app/scraper/playwright_utils.py`. This prevents event loop conflicts with Uvicorn's default `SelectorEventLoop` on Windows.

**Asus scraper dual-path**: ROG pages (`rog.asus.com`) read `window.__NUXT__.state.Spec.spec`; standard ASUS pages (`www.asus.com`) read `window.__NUXT__.state.PDPage`. Both return a `list[dict]` — one entry per SKU variant — with `?v=N` URL suffix for multi-variant pages.

**AI Processor model**: `gemma-4-31b-it` via `ChatGoogleGenerativeAI` (LangChain). Free-tier limits: **15 RPM, 1500 RPD, unlimited TPM**. The processor enforces a 5-second inter-request delay and a default batch of 100 per run (~8 min) to stay safely under 15 RPM. Hard ceiling is 1500 per run (`?limit=1500`). AFC (Automatic Function Calling) is enabled by the Google SDK — it self-corrects structured output against `ExtractedLaptopFamily` for up to 10 rounds per request.

**AI Processor safety rules**: The Gemini prompt enforces combinatorial safety (base configs first) and price matrix isolation (output `0.0` for upgrade prices not explicitly stated) to prevent hallucinated pricing. Duplicate SKUs are handled via an explicit **upsert**: query by `model_code` first — if exists, update all fields in-place and record price history only if `price_rm` changed and is non-zero; if new, insert and record the initial price snapshot. `model_code` is never overwritten during an update (it is the lookup key).

**Auth scopes**: JWT tokens are scoped — `email_verification` (1hr), `password_reset` (15min), and standard access tokens (7 days default). The `get_current_admin` dependency enforces `role == "admin"` (403 otherwise).

**Hybrid vector search** (`POST /laptops/hybrid-search` in `laptop_router.py`): embeds the user query with the same `gemini-embedding-001` model used for the documents, then runs a single SQL statement that joins `laptops` ↔ `laptop_embeddings` ↔ `laptop_brands`, orders by pgvector cosine distance (`<=>`, ascending = closest), and applies optional `budget_max`/`brand` hard filters in the same query. Distance is converted to `similarity_score = 1 − distance` in the response (higher = better). Returns a ranked candidate pool (`LaptopSearchResult`) intended to feed the downstream rerank → PickScore → LLM stages.

**Price history**: `Laptop` price is captured into `laptop_price_history` in three places: (1) `POST /laptops/` router on manual create, (2) `PUT /laptops/{id}` router when `price_rm` changes, and (3) the AI processor (`app/processor/engine.py`) on every new insert and on re-process when `price_rm` changes and is non-zero. `GET /laptops/{id}/price-history` returns the ordered series. This prevents vendor price changes from being silently lost and backs the planned Price History Tracker chart.

### Database Tables

All tables use UUID primary keys. Key relationships:
- `laptops` → `laptop_brands` (FK: `brand_id`)
- `laptops` → `laptop_customizations` (1:N)
- `laptops` → `laptop_embeddings` (1:1, 768-dim pgvector)
- `laptops` → `laptop_price_history` (1:N; snapshot on create + on price change)
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
- `conversation_laptops` → `conversations` + `laptops`; the agent's current shortlist pool for a thread — replaced whenever `search_laptops` returns a high-confidence result, otherwise left as-is for follow-up context
- `pipeline_eval_logs` → `users` + `conversations`; one row per live user request — `gate_status`, `top_score`, `relaxed_field`, `relaxed_from`, `relaxed_to`, `bottleneck`, `candidate_count`, `result_laptop_ids`

**YouTube Reviews:**
- `youtube_channels` — `channel_id`, `channel_name`, `channel_img_url`, `trust_tier` (`tier_1`/`tier_2`), `active`
- `raw_youtube_reviews` → `laptops` (FK: `matched_laptop_id`, nullable); `video_id`, `raw_transcript` (JSONB), `match_confidence`, `status` (`pending`/`matched`/`rejected`)
- `laptop_review_chunks` → `laptops` + `raw_youtube_reviews`; `chunk_text` (LLM summary), `embedding` (768-dim pgvector), `sentiment_tag`, `timestamp_start/end_seconds` (used for YouTube timestamp links in `get_review_evidence`)
- `laptop_review_summary` → `laptops` (1:1); `aggregated_strengths`/`aggregated_weaknesses` (JSONB top-5 each), `review_count`

### API Access Control

All routes are served under `/api/v2` (added via `prefix="/api/v2"` on every `app.include_router(...)` call in `app/main.py`, not on the individual routers themselves — e.g. `/laptops` in this doc means `GET /api/v2/laptops`). `GET /`, `/docs`, `/redoc`, `/openapi.json` are unprefixed. The OAuth2 password-flow `tokenUrl` in `app/users/auth.py` is `"api/v2/auth/login"` to match, for Swagger UI's Authorize button.

- Public endpoints: GET laptops (incl. `POST /laptops/hybrid-search` and `GET /laptops/{id}/price-history`), GET brands, GET benchmarks, GET product-types, GET categories, GET questionnaire, auth registration/login/verify
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
- **General** — triggered when `user_id` is null; uses `DEFAULT_PRIORITY` (N-i rule, 8 factors), neutral modifiers, inverse min-max for price, fixed 50 for brand/screen size

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
