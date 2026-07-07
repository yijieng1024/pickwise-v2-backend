# PickWise v2 — Backend

Backend for **PickWise v2**, a conversational laptop-recommendation platform for the Malaysian market. A LangGraph ReAct agent reasons over hybrid vector search, a deterministic **PickScore** ranking engine, YouTube review evidence, and pricing data to deliver personalized, explainable recommendations.

Built with **FastAPI + SQLModel + PostgreSQL (pgvector)**, powered by **Google Gemini** for extraction, embeddings, and conversation.

## Features

- **Conversational agent** (`POST /api/v2/agent/chat`) — LangGraph ReAct agent with tools for laptop search, custom Apple pricing, review evidence, and Malaysian market price lookup. Conversation threads and the per-thread laptop shortlist are persisted.
- **CRS retrieval pipeline** — retrieve (pgvector top-50) → rerank → stepwise constraint relaxation → confidence gating, exposed to the agent as the `search_laptops` tool. Every call is logged for evaluation (`pipeline_eval_logs` + JSONL traces).
- **PickScore engine** — deterministic, product-agnostic 8-factor scoring (price, CPU, GPU, RAM/storage, portability, battery, screen size, brand) with a 3-layer weighting pipeline; personalized via user preferences or general mode. No LLM involved — its structured breakdown feeds the LLM's explanations.
- **Recommendations** (`POST /api/v2/recommendations/laptops`) — hybrid vector search → batch PickScore → Gemini structured output, adapted to the user's tech-savviness.
- **Data ingestion pipeline** — Playwright scrapers (Apple DOM, Asus/ROG `window.__NUXT__`) → raw scrape store → Gemini-powered AI processor that normalizes multi-variant listings into a 9-part laptop spec model, with price-history tracking and upsert-by-`model_code`.
- **YouTube review ingestion** — channel discovery (YouTube Data API v3) → transcript fetch → RapidFuzz title matching → 45s chunk summarization + sentiment tagging + embedding → per-laptop strengths/weaknesses aggregation.
- **Benchmarks** — PassMark CPU/GPU scraping with PostgreSQL upsert, consumed by PickScore via fuzzy model matching.
- **Auth** — JWT (scoped tokens for email verification / password reset / access), bcrypt, role-based admin access, user preference questionnaire.

## Architecture

```
Feed Crawler ──► laptop_scrape_urls
                      │
Scrape / Bulk Scrape ─▼──► raw_scrap_laptops (pending)
                      │
AI Processor (Gemini) ─▼──► laptops (normalized, multi-variant)
                      │
Embeddings (gemini-embedding-001, 768-dim) ──► laptop_embeddings
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
| `app/agent/` | LangGraph ReAct agent — the sole conversational entry point |
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
| Bearer token | `/auth/me/*`, `POST /laptops/calculate-score[,/batch]`, `POST /recommendations/laptops`, `/conversations/*`, `POST /agent/chat` |
| Admin (`role == "admin"`) | All write operations: laptops, brands, customizations, scraper, processor, benchmarks, embeddings, taxonomy, and all `/reviews/*` endpoints |

## Docker & deployment

**Local container:**

```bash
docker compose up -d --build
```

Builds the image from source and runs the API on port 8000 (`.env` is loaded via `env_file`; the container runs `alembic upgrade head` before starting Uvicorn). The database is not part of the compose stack — point `DATABASE_URL` at your own Postgres instance.

**CI/CD:** pushing to `main` triggers `.github/workflows/deploy.yml`, which builds and pushes the image to GHCR (`ghcr.io/yijieng1024/pickwise-v2-backend`), then SSHes into the VPS and runs `docker compose -f docker-compose.prod.yml pull && up -d`. The VPS only needs `docker-compose.prod.yml` + `.env` — not the repo.

## Tech stack

- **API:** FastAPI, Uvicorn, SQLModel/SQLAlchemy, Alembic, Pydantic v2
- **Database:** PostgreSQL + pgvector (768-dim embeddings, cosine distance)
- **AI:** LangChain + LangGraph, Google Gemini (chat, structured extraction, `gemini-embedding-001`)
- **Scraping:** Playwright (Chromium), youtube-transcript-api, YouTube Data API v3
- **Matching:** RapidFuzz (benchmark lookup, review-to-laptop matching)
- **Auth:** PyJWT (scoped tokens), bcrypt via passlib

## Development notes

- **Windows + Playwright:** async Playwright calls run on a dedicated worker thread with its own `ProactorEventLoop` (`app/scraper/playwright_utils.py`) to avoid conflicts with Uvicorn's `SelectorEventLoop`.
- **Logging:** `app/logger.py` sets up console + rotating file logs (`logs/app.log`); pipeline evaluation traces go to `logs/eval/pipeline_trace.jsonl`.
- **Standalone scripts:** import `LaptopCustomization` before `LaptopUserPreference` to satisfy SQLAlchemy mapper resolution (the server handles this via `main.py` importing all routers).

For deeper architectural details (PickScore factor logic, scraping status flow, key design decisions), see [CLAUDE.md](CLAUDE.md).
