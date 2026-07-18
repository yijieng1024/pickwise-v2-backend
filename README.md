# PickWise v2 — Backend

Backend for **PickWise v2**, a conversational laptop-recommendation platform for the Malaysian market. A LangChain ReAct agent (**Pico**) reasons over hybrid vector search, a deterministic **PickScore** ranking engine, YouTube review evidence, and pricing data to deliver personalized, explainable recommendations.

Built with **FastAPI + SQLModel + PostgreSQL (pgvector)**, powered by **Google Gemini** for extraction, embeddings, and conversation.

## Features

- **Conversational agent** (`POST /api/v2/agent/chat`) — ReAct agent with tools for laptop search, custom Apple pricing, review evidence, and Malaysian market price lookup. Search results carry a deterministic **PickScore (0–100)** with a top-factor summary, which the agent cites when presenting laptops; the chat response also returns a structured `laptops` shortlist (id, name, price, PickScore, similarity) so a frontend can render score badges — persisted per thread, so follow-up turns keep the shortlist. The system prompt enforces strict scope (laptop topics only) and factual grounding (every price and score cited must come from tool output, never model memory). `POST /api/v2/agent/chat/stream` is the SSE twin — token-by-token reply plus `thinking` (model reasoning deltas for a live thinking-flow UI), `tool` activity, and `turn_reset` events; shortlist cards carry the first catalog photo (`image_url`). Conversation threads support rename (`PATCH /conversations/{id}`) and shortlist restore (`GET /conversations/{id}/laptops`).
- **Saved laptops** (`/api/v2/saved/*`) — per-user wishlist: full-record listing, lightweight id lookup for heart-state, idempotent save/unsave.
- **Market price lookup** — two-layer tool: official catalog price + price history from the own DB, and live Malaysian retail listings (Shopee, Lazada, senQ, …) via Google Shopping, with accessory filtering, per-store diversity caps, and honest fallbacks when data is missing.
- **CRS retrieval pipeline** — retrieve (pgvector top-50) → rerank → stepwise constraint relaxation → confidence gating, exposed to the agent as the `search_laptops` tool. Every call is logged for evaluation (`pipeline_eval_logs` + JSONL traces).
- **PickScore engine** — deterministic, product-agnostic 8-factor scoring (price, CPU, GPU, RAM/storage, portability, battery, screen size, brand) with a 3-layer weighting pipeline; personalized via user preferences or general mode. No LLM involved — its structured breakdown feeds the LLM's explanations. Consumed by the recommendation pipeline (personalized), the standalone `/laptops/calculate-score` endpoints, and the agent's search results (general mode).
- **Recommendations** (`POST /api/v2/recommendations/laptops`) — hybrid vector search → batch PickScore → Gemini structured output, adapted to the user's tech-savviness.
- **Data ingestion pipeline** — Playwright scrapers (Apple DOM, Asus/ROG `window.__NUXT__`) → raw scrape store → Gemini-powered AI processor that normalizes multi-variant listings into a 9-part laptop spec model, with price-history tracking and upsert-by-`model_code`.
- **YouTube review ingestion** — channel discovery (YouTube Data API v3) → transcript fetch (optionally via a Webshare residential proxy — YouTube blocks datacenter IPs) → RapidFuzz title matching (+ manual pairing and `POST /reviews/rematch`) → 45s chunk summarization + sentiment tagging + embedding (`POST /reviews/process-bulk` for duplicate-safe batch runs) → per-laptop strengths/weaknesses aggregation.
- **Benchmarks** — PassMark CPU/GPU scraping with PostgreSQL upsert, consumed by PickScore via fuzzy model matching.
- **Auth** — JWT (scoped tokens for email verification / password reset / access), bcrypt, role-based admin access, user preference questionnaire.
- **Agent eval harness** (`eval/`) — 30 bilingual (中文/English/Manglish) test queries across 5 behavior categories, graded by deterministic rule checks plus an LLM judge that verifies factual grounding against raw tool outputs; run-to-run comparison for regression catching.

## Architecture

```
Feed Crawler ──► laptop_scrape_urls
                      │
Scrape / Bulk Scrape ─▼──► raw_scrap_laptops (pending)
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
| `app/laptops/` | Laptop models, brands, customizations, price history, hybrid search, PickScore adapter |
| `app/embeddings/` | Per-laptop document building + Gemini embedding generation |
| `app/reviews/` | YouTube review discovery, transcripts, matching, chunk processing, aggregation |
| `app/scraper/` | Playwright crawlers (Apple, Asus/ROG) + bulk scraping |
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
| `SMTP_USERNAME` / `SMTP_PASSWORD` | ✅ | Gmail SMTP credentials for verification emails |
| `SMTP_SERVER` / `SMTP_PORT` | — | Default `smtp.gmail.com:465` |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key (embeddings, processor, agent, reviews) |
| `YOUTUBE_API_KEY` | optional | YouTube Data API v3 key — server starts without it; review discovery endpoints return 400 until set |
| `SERP_API_KEY` | optional | SerpApi (serpapi.com) key for the market-price tool's live-listings layer (Google Shopping, Malaysia) — without it the tool answers from the catalog layer + marketplace search links |
| `GOOGLE_OAUTH_CLIENT_ID` | optional | Google Sign-In web client id — `POST /auth/google` returns 400 without it |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | optional | Webshare rotating-**residential** proxy for review transcript fetches — required on cloud hosts (YouTube IP-blocks datacenter ranges); direct connection when unset |

### Database migrations

```bash
alembic upgrade head                                  # apply all pending migrations
alembic revision --autogenerate -m "description"      # create a new migration
alembic downgrade -1                                  # roll back one migration
```

## API overview

All routes live under `/api/v2` (e.g. `GET /api/v2/laptops`). `GET /` is an unprefixed health check.

| Access | Endpoints |
|---|---|
| Public | GET laptops, hybrid search, price history, brands, benchmarks, product types, categories, questionnaire; auth register/login/verify |
| Bearer token | `/auth/me/*`, `POST /laptops/calculate-score[,/batch]`, `POST /recommendations/laptops`, `/conversations/*` (incl. rename + `/{id}/laptops`), `/saved/*`, `POST /agent/chat[,/stream]` |
| Admin (`role == "admin"`) | All write operations: laptops, brands, customizations, scraper, processor, benchmarks, embeddings, taxonomy, and all `/reviews/*` endpoints |

## Docker & deployment

**Local container:**

```bash
docker compose up -d --build
```

Builds the image from source and runs the API on port 8000 (`.env` is loaded via `env_file`; the container runs `alembic upgrade head` before starting Uvicorn). The database is not part of the compose stack — point `DATABASE_URL` at your own Postgres instance.

**CI/CD:** pushing to `main` (documentation-only changes are excluded) triggers `.github/workflows/deploy.yml`, which builds the image with a GitHub Actions layer cache and pushes it to GHCR (`ghcr.io/yijieng1024/pickwise-v2-backend`), then fires the Render deploy hook to roll out the new image. `docker-compose.prod.yml` remains available as a self-hosted (VPS) alternative that pulls the same GHCR image — the host only needs that file + `.env`, not the repo.

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
- **Scraping:** Playwright (Chromium), youtube-transcript-api, YouTube Data API v3
- **Matching:** RapidFuzz (benchmark lookup, review-to-laptop matching)
- **Auth:** PyJWT (scoped tokens), bcrypt via passlib

## Development notes

- **Windows + Playwright:** async Playwright calls run on a dedicated worker thread with its own `ProactorEventLoop` (`app/scraper/playwright_utils.py`) to avoid conflicts with Uvicorn's `SelectorEventLoop`.
- **Logging:** `app/logger.py` sets up console + rotating file logs (`logs/app.log`); pipeline evaluation traces go to `logs/eval/pipeline_trace.jsonl`. File logging is best-effort — if `logs/` isn't writable (read-only container fs), it falls back to console instead of crashing startup.
- **Standalone scripts:** import `LaptopCustomization` before `LaptopUserPreference` to satisfy SQLAlchemy mapper resolution (the server handles this via `main.py` importing all routers).

For deeper architectural details (PickScore factor logic, scraping status flow, key design decisions), see [CLAUDE.md](CLAUDE.md).
