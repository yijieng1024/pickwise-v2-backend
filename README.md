# PickWise v2 — Backend

Backend for **PickWise v2**, a conversational laptop-recommendation platform for the Malaysian market. A LangChain ReAct agent (**Pico**) reasons over hybrid vector search, a deterministic **PickScore** ranking engine, YouTube review evidence, and pricing data to deliver personalized, explainable recommendations.

Built with **FastAPI + SQLModel + PostgreSQL (pgvector)**, powered by **Google Gemini** for extraction, embeddings, and conversation.

## Features

- **Conversational agent** (`POST /api/v2/agent/chat`) — ReAct agent with tools for laptop search, custom Apple pricing, review evidence, and Malaysian market price lookup. Search results carry a deterministic **PickScore (0–100)** with a top-factor summary, which the agent cites when presenting laptops; the chat response also returns a structured `laptops` shortlist (id, name, price, PickScore, similarity) so a frontend can render score badges — persisted per thread, so follow-up turns keep the shortlist. The system prompt enforces strict scope (laptop topics only) and factual grounding (every price and score cited must come from tool output, never model memory). `POST /api/v2/agent/chat/stream` is the SSE twin — token-by-token reply plus `thinking` (model reasoning deltas for a live thinking-flow UI), `tool` activity, and `turn_reset` events; shortlist cards carry the first catalog photo (`image_url`). Conversation threads support rename (`PATCH /conversations/{id}`) and shortlist restore (`GET /conversations/{id}/laptops`).
- **Saved laptops** (`/api/v2/saved/*`) — per-user wishlist: full-record listing, lightweight id lookup for heart-state, idempotent save/unsave.
- **Laptop families** (`/api/v2/families/*`) — the catalog stores one row per configuration, so a single machine can occupy 14 rows and fill a shortlist on its own. `laptop_family` groups them at product-line granularity, and both the agent's `search_laptops` and the use-case ranking deduplicate on it (picking the member closest to the stated budget). Seeded automatically (`POST /families/regroup`, unassigned rows only), merged by hand through the CRUD — a new laptop whose grouping is ambiguous stays unassigned rather than being guessed into the wrong family.
- **Listing status** — `Laptop.status` (`active` / `inactive` / `suspended`) is the listing state; only `active` laptops are recommendable, enforced inside the retrieval module so relaxation retries and eval re-entry stay filtered. A laptop with no price (`price_rm = 0` — the schema's "unknown") is demoted to `inactive` automatically on every write, so an unpriced row lands in the awaiting-a-price queue instead of being offered as trivially affordable; `suspended` rows are left alone and restoring a price does not re-activate. Retiring a machine is `suspended`, not a delete: `DELETE /laptops/{id}` refuses with 409 while user- or pipeline-owned rows (saved laptops, conversation shortlists, review chunks/summaries, matched raw reviews) still reference it.
- **Market price lookup** — two-layer tool: official catalog price + price history from the own DB, and live Malaysian retail listings (Shopee, Lazada, senQ, …) via Google Shopping, with accessory filtering, per-store diversity caps, and honest fallbacks when data is missing.
- **Retrieval pipeline** — retrieve (pgvector top-50) → rerank → stepwise constraint relaxation → confidence gating, exposed to the agent as the `search_laptops` tool. Every call is logged for evaluation (`pipeline_eval_logs` + JSONL traces).
- **PickScore engine** — deterministic, product-agnostic 8-factor scoring (price, CPU, GPU, RAM/storage, portability, battery, screen size, brand) with a 3-layer weighting pipeline; personalized via user preferences or general mode. Factors normalize by **percentile rank against the catalog's own distribution**, not min-max between two outliers, so a score reads as "better than N% of the catalog" and the preset weights actually mean what they say — regenerate general-mode scores (`POST /laptops/pick-scores/generate-all`) after catalog changes, not just after benchmark or weight changes. No LLM involved — its structured breakdown feeds the LLM's explanations. A benchmark that cannot be resolved is flagged as a fabricated neutral rather than passed off as a measurement, and when both CPU and GPU are unresolved the total is **withheld** (`score: null`, full breakdown still returned) instead of published as a number half-built from guesses. Consumed by the recommendation pipeline (personalized), the standalone `/laptops/calculate-score` endpoints, and the agent's search results (general mode).
- **Recommendations** (`POST /api/v2/recommendations/laptops`) — hybrid vector search → batch PickScore → Gemini structured output, adapted to the user's tech-savviness.
- **Data ingestion pipeline** — Playwright scrapers (Apple DOM, Asus/ROG `window.__NUXT__`) → raw scrape store → Gemini-powered AI processor that normalizes multi-variant listings into a 9-part laptop spec model, with price-history tracking and upsert-by-`model_code`.
- **Uploaded-HTML ingestion** (`POST /api/v2/scraper/upload-html`) — for storefronts that cannot be scraped at all: Acer's store sits behind Akamai Bot Manager, which refuses automated clients outright. Pages are saved by hand from a normal browser and posted to the API; each identifies itself by its `<link rel="canonical">` tag, so filenames are irrelevant and the page is matched back to its queued target automatically. HTML is stored in Postgres (`raw_product_htmls` — the container filesystem is ephemeral) and parsed with lxml through the same downstream pipeline as any scraped brand. The table is product-agnostic, ready for monitors/desktops without a schema change.
- **YouTube review ingestion** — channel discovery (YouTube Data API v3) → transcript fetch (optionally via a Webshare residential proxy — YouTube blocks datacenter IPs) → RapidFuzz title matching (+ manual pairing and `POST /reviews/rematch`) → 45s chunk summarization + sentiment tagging + embedding (`POST /reviews/process-bulk` for duplicate-safe batch runs) → per-laptop strengths/weaknesses aggregation.
- **Review triage** — transcript failures are typed (`no_track`, `video_unavailable`, `ip_blocked`, `network`, `unknown`), so a proxy hiccup is no longer indistinguishable from captions being disabled; `POST /reviews/retry-transcripts` re-fetches only the retryable ones at zero YouTube quota. Off-topic videos are dismissed with `PATCH /reviews/raw/{id}/irrelevant` (marked, never deleted — `video_id` is unique and a deleted row is rediscovered on the next ingest).
- **Review→configuration linking** (`GET /reviews/families/{id}/configs`) — reviews attach to a family first, then optionally to one configuration. The endpoint reports whether the configurations are separable at all (if they differ only by RAM/storage, no reviewer could say which they tested, so the UI asks nothing) and, given `?review_id=`, returns spec-string evidence found in the video description or transcript with timestamps — so the choice is a confirmation, not a guess.
- **Benchmarks** — PassMark CPU/GPU scraping with PostgreSQL upsert, consumed by PickScore via fuzzy model matching.
- **Auth** — JWT (scoped tokens for email verification / password reset / access), bcrypt, role-based admin access, user preference questionnaire. Admin role/status changes are guarded against lockout: no self-demotion, and the last active admin cannot be demoted or deactivated.
- **Background jobs** — long batch operations (bulk scrape, AI processing, category backfill) return `202 Accepted` with a `job_id` instead of holding the connection open for minutes. Poll `GET /api/v2/jobs/{job_id}` for live counts, per-item errors and a progress percentage; job state lives in Postgres, so it survives a deploy and interrupted runs are failed on the next startup rather than sticking in `processing`.
- **Agent run monitoring** (`/api/v2/agent/monitoring/*`, admin) — per-turn run logs with tool calls, errors and aggregate stats.
- **Agent eval harness** (`eval/`) — 30 bilingual (中文/English/Manglish) test queries across 5 behavior categories, graded by deterministic rule checks plus an LLM judge that verifies factual grounding against raw tool outputs; run-to-run comparison for regression catching.

## Architecture

```
Feed Crawler ──► laptop_scrape_urls ◄── upload-html (canonical-URL match)
                      │                        │
                      │                        ▼
                      │                 raw_product_htmls   (WAF-blocked brands)
                      │                        │
Scrape / Bulk Scrape ─▼────────────────────────▼──► raw_scrap_laptops (pending)
                      │
AI Processor (Gemini) ─▼──► laptops (normalized, multi-variant)
                      │
Embeddings (gemini-embedding-2, 768-dim) ──► laptop_embeddings
                                                    │
User ──► /agent/chat ──► LangGraph agent ──► search_laptops tool
                              │                (retrieve → rerank → relax → gate)
                              ├──► PickScore engine (deterministic ranking)
                              └──► review evidence / market price tools
```

Independent pipelines: PassMark benchmark scraping (`cpu_benchmarks` / `gpu_benchmarks`) and YouTube review ingestion (`app/reviews/`).

### Module layout

| Module | Responsibility |
|---|---|
| `app/agent/` | LangChain ReAct agent (`create_agent`) — the sole conversational entry point, plain + SSE streaming |
| `app/saved/` | Per-user saved-laptops wishlist |
| `app/rag/` | CRS pipeline modules (retrieval, rerank, relaxation, gating, evaluation) + conversation-thread CRUD |
| `app/recommendation/` | One-shot recommendation pipeline (search → PickScore → LLM) |
| `app/pickscore/` | Product-agnostic scoring engine (adapter pattern for future 3C categories) |
| `app/common/` | Background-job model, service and `/jobs` polling router |
| `app/laptops/` | Laptop models, brands, families, customizations, price history, hybrid search, PickScore adapter |
| `app/embeddings/` | Per-laptop document building + Gemini embedding generation |
| `app/reviews/` | YouTube review discovery, transcripts, matching, chunk processing, aggregation |
| `app/scraper/` | Playwright crawlers (Apple, Asus/ROG) + bulk scraping + uploaded-HTML ingestion & offline parsing (Acer) |
| `app/processor/` | LLM extraction from raw scrapes into structured laptops |
| `app/benchmark/` | PassMark CPU/GPU scrapers |
| `app/taxonomy/` | Product types + marketing/use-case categories |
| `app/users/` | Auth, JWT, email verification, preferences, questionnaire |

Each domain module follows the same pattern: `models.py` (SQLModel tables + Pydantic schemas), `router.py` (FastAPI endpoints), plus supporting services.

## Getting started

### Prerequisites

- Python 3.11+
- PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension
- A Google Gemini API key

### Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browsers (needed for the scrapers)
playwright install chromium

# 4. Configure environment
copy .env.example .env         # then fill in the values (see below)

# 5. Apply database migrations
alembic upgrade head

# 6. Run the development server
uvicorn app.main:app --reload
```

The API is served at `http://localhost:8000`, with interactive docs at `/docs`.

### Environment variables

See `.env.example` and `app/config.py`:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres connection string (pgvector must be available) |
| `SECRET_KEY` | ✅ | JWT signing key |
| `ALGORITHM` | — | JWT algorithm (default `HS256`) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | — | Access-token TTL (default 10080 = 7 days) |
| `BREVO_API_KEY` | ✅ | Brevo HTTP API key for transactional email (verification, password reset) |
| `EMAIL_SENDER_ADDRESS` | — | From-address; must be a **verified sender** in Brevo (default `noreply@ngyijie.com`) |
| `EMAIL_SENDER_NAME` | — | From-name (default `PickWise`) |
| `FRONTEND_URL` / `BACKEND_URL` | — | Base URLs for links inside emails (password reset / verification) — default to `localhost` if unset |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | — | Connection-pool sizing (default 10 + 20) |
| `DB_POOL_TIMEOUT` / `DB_POOL_RECYCLE` | — | Seconds waiting for a free connection (30) and idle-connection recycle age (1800) |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key (embeddings, processor, agent, reviews) |
| `YOUTUBE_API_KEY` | optional | YouTube Data API v3 key — server starts without it; review discovery endpoints return 400 until set |
| `SERP_API_KEY` | optional | SerpApi (serpapi.com) key for the market-price tool's live-listings layer (Google Shopping, Malaysia) — without it the tool answers from the catalog layer + marketplace search links |
| `TEST_DATABASE_URL` | tests | Postgres+pgvector used by the integration tier and by Alembic unless `ALEMBIC_TARGET=production` |
| `GOOGLE_OAUTH_CLIENT_ID` | optional | Google Sign-In web client id — `POST /auth/google` returns 400 without it |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | optional | Webshare rotating-**residential** proxy for review transcript fetches — required on cloud hosts (YouTube IP-blocks datacenter ranges); direct connection when unset |

### Database migrations

```bash
alembic upgrade head                                  # apply all pending migrations
alembic check                                         # show drift without writing a file
alembic revision --autogenerate -m "description"      # create a new migration
alembic downgrade -1                                  # roll back one migration
```

> **Before trusting an autogenerated migration, run `alembic check`.** Autogenerate diffs the live database against `SQLModel.metadata`, so a table whose model module is missing from `alembic/env.py` looks deleted and gets a `DROP TABLE`. Every table module must be imported there — importing any one name from a module registers all of its tables. `alembic check` should print *"No new upgrade operations detected."*

## API overview

All routes live under `/api/v2` (e.g. `GET /api/v2/laptops`). `GET /` is an unprefixed health check.

| Access | Endpoints |
|---|---|
| Public | GET laptops (`?status=` filters; omitted = all), hybrid search, price history, PickScore reads + use-case ranking, brands, families, benchmarks, product types, categories, questionnaire; auth register/login/verify |
| Bearer token | `/auth/me/*`, `POST /laptops/calculate-score[,/batch]`, `POST /recommendations/laptops`, `/conversations/*` (incl. rename + `/{id}/laptops`), `/saved/*`, `POST /agent/chat[,/stream]` |
| Admin (`role == "admin"`) | All write operations: laptops, brands, families (incl. membership moves and `POST /families/regroup`), customizations, scraper (incl. `POST /scraper/upload-html` and `/upload-html/json`), processor, benchmarks, embeddings, taxonomy, users, `/jobs/*`, `/agent/monitoring/*`, and all `/reviews/*` endpoints |

Four admin endpoints are **asynchronous**: `POST /scraper/bulk-scrape`, `POST /scraper/scrape-targets`, `POST /processor/process-pending` and `POST /processor/categorize-untagged` return `202 Accepted` with a `job_id` and `poll_url`. Poll `GET /jobs/{job_id}` until `status` is `completed` or `failed`; the finished job's `result` contains the full report those endpoints used to return synchronously. Per-item failures are reported in `failed_count`/`errors[]` and do **not** fail the job.

## Docker & deployment

**Local container:**

```bash
docker compose up -d --build
```

Builds the image from source and runs the API on port 8000 (`.env` is loaded via `env_file`; the container runs `alembic upgrade head` before starting Uvicorn). The database is not part of the compose stack — point `DATABASE_URL` at your own Postgres instance.

**CI/CD:** pushing to `main` (documentation-only changes are excluded) triggers `.github/workflows/deploy.yml`, which builds the image with a GitHub Actions layer cache and pushes it to GHCR (`ghcr.io/yijieng1024/pickwise-v2-backend`), then fires the Render deploy hook to roll out the new image. `docker-compose.prod.yml` remains available as a self-hosted (VPS) alternative that pulls the same GHCR image — the host only needs that file + `.env`, not the repo.

## Testing

```bash
pytest tests/ -q                                             # everything
pytest tests/unit tests/test_golden_pickscore.py -q          # fast tier (~2s)
pytest tests/test_golden_pickscore.py --update-golden        # re-snapshot PickScore
```

Two tiers, kept in separate CI jobs:

- **Unit + golden** — no database, no network, no API key, ~2 seconds. That speed is the whole point, so it runs in CI with **no secrets at all**; `tests/unit/test_no_secrets_required.py` re-imports the tier in a subprocess with every secret unset to keep the property honest. The golden PickScore snapshot asserts that score changes were deliberate (its numbers are percentiles over 20 fixture laptops — never compare them with production scores).
- **Integration** — needs Postgres with pgvector. Uses `TEST_DATABASE_URL`, else starts a `pgvector/pgvector` testcontainer, else **skips with instructions** rather than passing having run nothing. Migrations never target production by default: `alembic/env.py` requires `TEST_DATABASE_URL` unless `ALEMBIC_TARGET=production` (which the Dockerfile's start command sets).

CI (`.github/workflows/ci.yml`) runs `unit`, `integration` and a path-filtered `migrations` job; `nightly-eval.yml` runs the agent eval on a schedule and gates nothing (run-to-run variance is too high for a merge check).

## Agent evaluation

`eval/queries.yaml` defines 30 bilingual queries across 5 behavior categories — clear intent, vague intent, constraint relaxation, relevance gating (scope/refusals), and tool routing. Each case declares required/forbidden tools, budget caps, and a grading rubric.

```bash
# from the project root
python eval/run_eval.py run --label baseline                   # full run with LLM judge
python eval/run_eval.py run --label quick --no-judge           # rule checks only (free)
python eval/run_eval.py run --label x --only relevance_gating  # one category
python eval/run_eval.py run --label x --ids relax_zh_003       # specific cases
python eval/run_eval.py compare eval/runs/A.jsonl eval/runs/B.jsonl   # regression diff
```

The harness calls the agent in-process with the exact production model and system prompt, applies deterministic rule checks (tools called, budget respected, non-empty reply), then an LLM judge that grades the rubric **against the raw tool outputs only** — the judge is forbidden from using its own product knowledge, so honest answers about catalog data never get marked wrong by a stale model. All LLM calls share a rate limiter tuned to the Gemini free tier. Results are saved as JSONL per run for `compare`.

## Tech stack

- **API:** FastAPI, Uvicorn, SQLModel/SQLAlchemy, Alembic, Pydantic v2
- **Database:** PostgreSQL + pgvector (768-dim embeddings, cosine distance)
- **AI:** LangChain (`create_agent` ReAct loop), Google Gemini/Gemma (agent + extraction + review chunking: `gemma-4-31b-it`; embeddings: `gemini-embedding-2`)
- **Scraping:** Playwright (Chromium), lxml (offline HTML parsing), youtube-transcript-api, YouTube Data API v3
- **Matching:** RapidFuzz (benchmark lookup, review-to-laptop matching)
- **Auth:** PyJWT (scoped tokens), bcrypt via passlib

## Development notes

- **Windows + Playwright:** async Playwright calls run on a dedicated worker thread with its own `ProactorEventLoop` (`app/scraper/playwright_utils.py`) to avoid conflicts with Uvicorn's `SelectorEventLoop`.
- **Logging:** `app/logger.py` sets up console + rotating file logs (`logs/app.log`); pipeline evaluation traces go to `logs/eval/pipeline_trace.jsonl`. File logging is best-effort — if `logs/` isn't writable (read-only container fs), it falls back to console instead of crashing startup.
- **Standalone scripts:** every model module is importable on its own (deferred bottom-of-module imports resolve the `Laptop` ↔ `LaptopCustomization` ↔ `Category` string relationships), so scripts and tests need no import-order workaround. Keep it that way when adding a relationship whose target lives in another module.
- **Connection pool:** never hold a pooled connection across slow non-database work. The chat endpoints take no `Depends(get_session)` — a session lives until the response completes, which for SSE means the last byte — and bracket their reads/writes in `session_scope()` instead. Pool size is configurable (`DB_POOL_SIZE` / `DB_MAX_OVERFLOW`).
- **Email goes over Brevo's HTTP API, not SMTP:** Render's instances block outbound SMTP ports (25/465/587), which used to make verification/reset mail fail silently in production while registration still returned 201. `app/users/email.py` now POSTs to `api.brevo.com` on 443. The sender address must be a *verified sender* in Brevo, not just an address on the authenticated domain.
- **Auth flows are enumeration-resistant and rate-limited:** the email-taking endpoints return one generic response whatever happened, reset links are single-use (an HMAC of the current password hash is baked into the token), and `app/common/http_rate_limit.py` caps each endpoint per IP *and* per account/address. See CLAUDE.md for the limits and the `X-Forwarded-For` caveat.

For deeper architectural details (PickScore factor logic, scraping status flow, key design decisions), see [CLAUDE.md](CLAUDE.md). Decisions with a rationale worth keeping are recorded as ADRs in [`docs/adr/`](docs/adr) — PickScore positioning, percentile normalization, the two-kinds-of-hidden status column, benchmark resolution, review linkage, and non-English review coverage.
