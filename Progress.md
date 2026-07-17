# Pickwise Backend - Development Progress

## Project Overview

A FastAPI-based backend for a laptop recommendation system with user authentication, profile management, preference tracking, web scraping pipeline, AI-powered data processing, benchmark scoring, and comprehensive laptop catalog management.

---

## 🔧 Technical Stack

- **Framework**: FastAPI (v2.0)
- **Database**: PostgreSQL 16 with pgvector extension (via Docker)
- **ORM**: SQLModel (SQLAlchemy + Pydantic hybrid)
- **Migrations**: Alembic
- **Authentication**: JWT (PyJWT) with OAuth2 PasswordBearer (username-or-email login) + Google Sign-In (ID-token verification via `google-auth`)
- **Password Hashing**: bcrypt (via passlib)
- **Email**: SMTP (Gmail SSL, port 465) with background tasks
- **Web Scraping**: Playwright (Chromium headless)
- **AI/LLM Processing**: LangChain + **Google Gemini/Gemma** (agent: `gemma-4-31b-it` via `ChatGoogleGenerativeAI`; embeddings: `gemini-embedding-2`, `output_dimensionality=768`; extraction + category tagging: `gemma-4-31b-it`; recommendation + review chunking: `gemini-3.5-flash`; eval judge)
- **Vector Storage**: pgvector (768-dimension embeddings)
- **Agentic Orchestration**: `langchain.agents.create_agent` (migrated off the deprecated `langgraph.prebuilt.create_react_agent`)
- **YouTube Ingest**: `google-api-python-client` (YouTube Data API v3) + `youtube-transcript-api` v1.x
- **Validation**: Pydantic v2 with custom field validators
- **Settings**: pydantic-settings with `.env` file
- **Containerization**: Docker Compose (pgvector/pgvector:pg16)

---

## 📁 Project Structure

```
pickwise-v2-backend/
│
├── 📂 .vscode/                      # VS Code workspace settings
│   └── 📄 settings.json
│
├── 📂 alembic/                      # Database migration framework
│   ├── 📂 versions/                # Database migration scripts (UUID-based history)
│   ├── 📄 env.py                    # Alembic migration environment config
│   ├── 📄 script.py.mako           # Migration template
│   └── 📄 README                   # Alembic readme
│
├── 📂 app/                          # Main application package
│   ├── 📂 benchmark/               # CPU & GPU benchmark scoring module (PassMark scrapers)
│   ├── 📂 agent/                    # Agentic layer — 4 tools: search_laptops (+ PickScore), apple price, review evidence, market price
│   ├── 📂 reviews/                  # YouTube review ingestion pipeline (discovery → transcript → match → chunk → embed)
│   ├── 📂 rag/                      # CRS pipeline library (retrieve/rerank/relax/gate/evaluate) + conversation-thread models (renamed from conversations/)
│   ├── 📂 laptops/                  # Laptop catalog, brands & customizations module
│   ├── 📂 processor/               # AI-powered data processor (LLM extraction via Gemini)
│   ├── 📂 scraper/                  # Playwright web scraping pipeline (Apple & Asus specs)
│   │   ├── 📄 apple_scraper.py     # Apple-specific crawler and spec extractor (async)
│   │   ├── 📄 asus_scraper.py      # Asus/ROG-specific __NUXT__ JSON extractor (variant-aware)
│   │   ├── 📄 bulk_scraper.py      # Bulk scraping orchestration module (brand-wide)
│   │   ├── 📄 models.py            # ScrapeTarget & RawScrapLaptop models
│   │   ├── 📄 playwright_utils.py  # Thread runner for async Playwright (Windows compat)
│   │   └── 📄 router.py            # Scraper API endpoints
│   ├── 📂 users/                    # User authentication, profiles, and preferences module
│   ├── 📄 config.py                 # Application settings and environment variables loader
│   ├── 📄 database.py              # SQLModel engine setup and database session generators
│   └── 📄 main.py                   # FastAPI application initialization and routing
│
├── 📂 eval/                         # Offline agent eval harness (not part of the server)
│   ├── 📄 queries.yaml              # 30 bilingual test queries × 5 behavior categories
│   ├── 📄 run_eval.py               # Rule checks + grounded LLM judge; run/compare commands
│   └── 📂 runs/                     # JSONL results per labeled run
│
├── 📂 logs/                         # Runtime logs
│   ├── 📂 scraper/                  # Timestamped bulk scrape failure logs
│   └── 📂 eval/                     # pipeline_trace.jsonl — live CRS quality tracing
│
├── 📄 .gitignore                    # Git file exclusion rules
├── 📄 Progress.md                   # Project status and progress tracker (this file, gitignored)
├── 📄 alembic.ini                   # Alembic configuration file
└── 📄 docker-compose.yml            # PostgreSQL + pgvector container definition
```

---

## ✅ Completed Work Summary

### 1. **User Authentication System** (`app/users/`)

Core authentication features implemented for secure user registration, login, and role-based access control.

#### Files:
- `app/users/router.py` — All auth & user profile endpoints
- `app/users/auth.py` — JWT token utilities, password hashing, user extraction
- `app/users/models.py` — User, LaptopUserPreference, UserRead, Token models
- `app/users/schema.py` — Request/response schemas with validators
- `app/users/email.py` — HTML email templates (verification & password reset)

#### Endpoints Implemented:

| Method | Endpoint              | Auth              | Status | Purpose                   |
| ------ | --------------------- | ----------------- | ------ | ------------------------- |
| POST   | /auth/register        | None              | 201    | Register new user         |
| GET    | /auth/verify-email    | Query param token | 200    | Verify email address      |
| POST   | /auth/login           | OAuth2 form       | 200    | Get JWT token (username **or email** + password) |
| POST   | /auth/google          | Google ID token   | 200    | Sign in with Google (find-or-create + link by email) |
| GET    | /auth/me/profile      | Bearer Token      | 200    | Get user profile          |
| PUT    | /auth/me/profile      | Bearer Token      | 200    | Update user profile       |
| GET    | /auth/me/preferences  | Bearer Token      | 200    | Get laptop preferences    |
| PUT    | /auth/me/preferences  | Bearer Token      | 200    | Create/update preferences |
| PUT    | /auth/me/avatar       | Bearer Token      | 200    | Upload/replace avatar (JPEG/PNG/WebP ≤ 2 MB) |
| GET    | /auth/avatar/{user_id}| None              | 200    | Serve avatar image bytes  |
| DELETE | /auth/me/avatar       | Bearer Token      | 204    | Remove avatar             |
| POST   | /auth/forgot-password | None              | 202    | Request password reset    |
| POST   | /auth/reset-password  | Token in body     | 200    | Complete password reset   |

#### Key Features:
- **Registration**: Validates unique username/email, hashes password (bcrypt), sends verification email via background task. Usernames must not contain `@` (or be blank) — prevents a username from shadowing another user's email in the shared login lookup
- **Email Verification**: JWT-based token with configurable expiration (default: 1 hour), prevents login without verified email
- **Login**: Single identifier field accepts **username or email** (`(username == x) | (email == x)` on two unique indexed columns); validates credentials + verification status, generates JWT access token (default: 10080 min / 7 days). Guards `password IS NULL` (Google-only accounts can't log in with an empty password)
- **Google Sign-In** (`POST /auth/google`): frontend obtains an ID token via Google Identity Services and posts `{id_token}`; backend verifies signature/expiry/audience with `google-auth` against `GOOGLE_OAUTH_CLIENT_ID` (optional setting — endpoint 400s if unset). Resolution order: returning user by `provider_sub` (Google's stable `sub`) → existing local account by email (linked + marked verified, keeps its password) → new account (unique username generated from email prefix, `password=None`, `auth_provider="google"`, `is_verified=True` — no SMTP verification needed). Returns the same `Token` as `/auth/login`. Google-only users can later gain a password via the existing forgot-password flow
- **Profile Management**: Partial updates for birthday (date), gender (Male/Female/Other with validator), occupation
- **Preferences System**: Dedicated `laptop_user_preference` table with budget (`{min, max}` JSON RM range — `max: null` means no upper limit), purpose, priorities (weighted 1-10), screen_size, portability, brand_preferences, tech_savviness (validated enum). Written via the existing `PUT /me/preferences`; the 6-step survey that populates these fields is now served dynamically via `GET /questionnaire` (see §15 below) instead of being hardcoded in the frontend.
- **Password Reset**: JWT-based reset token (15 min expiry), safe messaging (doesn't reveal if user exists)
- **Avatar Gateway** (`user_avatars` table, 1:1 with users): image bytes stored as Postgres `bytea` — Render's filesystem is ephemeral so disk storage would be wiped every deploy, and no external object storage is configured. Separate table (not a column on `users`) so the blob is never loaded by the per-request `get_current_user` lookup. Upload validates by **magic bytes** (JPEG/PNG/WebP signatures — client Content-Type header is not trusted), 2 MB cap (413), unsupported type → 415. `GET /auth/avatar/{user_id}` is public (frontend uses it directly as `<img src>`) with `Cache-Control: public, max-age=300`. **Google avatar import**: on Google account creation/linking, the ID token's `picture` claim is fetched server-side (256px variant, 5s timeout) and stored through the same pipeline — best-effort, never blocks login, and only when the user has no avatar yet (an uploaded or deliberately deleted avatar is never overwritten; returning-user logins don't re-import)
- **Role-Based Access**: `get_current_user` and `get_current_admin` dependency functions for protected endpoints

---

### 2. **Laptop Catalog System** (`app/laptops/`)

Comprehensive 9-part laptop specification model with full CRUD operations.

#### Files:
- `app/laptops/laptop_models.py` — LaptopBase (9-part spec), Laptop, LaptopRead, LaptopCreate, LaptopUpdate, LaptopEmbedding
- `app/laptops/laptop_router.py` — Laptop CRUD endpoints

#### Endpoints Implemented:

| Method | Endpoint                    | Auth         | Status | Purpose                  |
| ------ | --------------------------- | ------------ | ------ | ------------------------ |
| POST   | /laptops/                   | None         | 201    | Create laptop            |
| GET    | /laptops/                   | None         | 200    | List all laptops         |
| GET    | /laptops/{laptop_id}        | None         | 200    | Get specific laptop      |
| PUT    | /laptops/{laptop_id}        | None         | 200    | Update laptop            |
| DELETE | /laptops/{laptop_id}        | None         | 204    | Delete laptop            |
| GET    | /laptops/raw-scrap-laptops  | Admin only   | 200    | List raw scraped data    |

#### Laptop Model — 9-Part Specification Schema:

| Part | Category                          | Key Fields |
| ---- | --------------------------------- | ---------- |
| 1    | Core Identifiers & Categorization | brand_id (FK), model_code (unique), product_name, release_year, price_rm |
| 2    | Processor & AI Engine             | processor_brand, processor_model, processor_ghz, cpu_cores, cpu_threads, npu_model, npu_tops, ai_ready, ai_features (JSONB) |
| 3    | Graphics & Hardware Acceleration  | gpu_brand, gpu_model, gpu_cores, media_engine_details |
| 4    | Memory & Storage                  | ram_gb, ram_type, ram_upgradable, max_ram_gb, ssd_gb, storage_type, storage_upgradable, expansion_slots_summary |
| 5    | Display & External Video          | display_size_inch, display_resolution, display_type, display_refresh_rate_hz, display_brightness_nits, touchscreen, external_display_support |
| 6    | Build, Battery & Connectivity     | weight_kg, dimensions_cm, battery_wh, power_supply_details, os, colors (JSONB), ports_summary (JSONB), wifi_standard, bluetooth_version |
| 7    | Peripherals, Input & Audio        | keyboard_touchpad_details, audio_details, camera_details, facial_recognition, fingerprint_reader |
| 8    | Security, Certifications & Extras | security_features, materials_and_certifications, microsoft_office_included, bundled_accessories, warranty_details |
| 9    | RAG & LLM Embedding Block         | raw_specs (JSONB), image_urls (JSONB) |

#### Additional Models:
- **LaptopEmbedding**: Vector embedding table (768-dim via pgvector) for AI/RAG-based similarity search, linked 1:1 with Laptop
- **RawScrapLaptop** (moved to `app/scraper/models.py`): Staging table for scraped data with processing_status ('pending', 'processing', 'completed', 'failed')

#### Import Architecture Fix:
- Uses `TYPE_CHECKING` guard in `laptop_models.py` and `customization_model.py` to prevent circular imports
- Deferred import of `LaptopCustomization` at bottom of `laptop_models.py` (after all classes defined) to register it in SQLAlchemy's mapper registry

---

### 3. **Brand Management System** (`app/laptops/brand_*`)

UUID-based brand system with dedicated CRUD operations and admin-only write access.

#### Files:
- `app/laptops/brand_model.py` — LaptopBrand, BrandBase, BrandCreate, BrandUpdate, BrandRead
- `app/laptops/brand_router.py` — Brand CRUD endpoints

#### Brand Model:
```
- id: UUID (primary key)
- name: str (unique, indexed)
- base_scrape_url: str
- icons_url: Optional[str]
- is_active: bool (default: True)
- created_at: datetime (auto-timestamp)
```

#### Endpoints Implemented:

| Method | Endpoint        | Auth       | Status | Purpose                 |
| ------ | --------------- | ---------- | ------ | ----------------------- |
| POST   | /brands         | Admin only | 201    | Create new brand        |
| GET    | /brands         | None       | 200    | List brands (paginated) |
| GET    | /brands/{id}    | None       | 200    | Get specific brand      |
| PUT    | /brands/{id}    | Admin only | 200    | Update brand            |
| DELETE | /brands/{id}    | Admin only | 204    | Delete brand            |

#### Key Features:
- Duplicate name checking (409 Conflict)
- Optional filtering by `is_active` status on list endpoint
- Offset/limit pagination
- Orphan checking on delete (409 if laptops still reference brand)

---

### 4. **Laptop Customization System** (`app/laptops/customization_*`)

Customization/upgrade tracking for laptops (e.g., RAM upgrades, storage options, Apple CTO configurations).

#### Files:
- `app/laptops/customization_model.py` — LaptopCustomization model, CustomizationRead, CustomizationBulkCreate, CustomizationUpdate schemas
- `app/laptops/customization_router.py` — Customization CRUD endpoints
- `app/laptops/customization_schema.py` — Additional schema definitions (CustomizationCreate, CustomizationBulkCreateByPattern, alternative schemas)

#### LaptopCustomization Model:
```
- id: UUID (primary key)
- laptop_id: UUID (foreign key → laptops.id, indexed)
- category_id: UUID (foreign key → categories.id) — was a free-typed `category: str` string; migrated to a FK into the shared `categories` taxonomy table (see §15) to fix drift risk (typo'd/duplicate category names). Existing string values were backfilled into `categories` by distinct name during the migration.
- option_name: str (e.g., "Upgrade to 24GB")
- price_add_rm: float (additional cost in RM)
- dependency_note: Optional[str] (e.g., "Requires M5 Pro chip")
- laptop: Relationship → Laptop (back_populates="customizations")
- category: Relationship → Category (back_populates="customizations")
```

#### Endpoints Implemented:

| Method | Endpoint                               | Auth       | Status | Purpose                       |
| ------ | -------------------------------------- | ---------- | ------ | ----------------------------- |
| POST   | /customizations/                       | Admin only | 201    | Bulk create customizations    |
| POST   | /customizations/bulk-by-pattern        | Admin only | 200    | Bulk create customizations by matching model_code pattern |
| GET    | /customizations/laptop/{laptop_id}     | Admin only | 200    | Get customizations by laptop  |
| PATCH  | /customizations/{customization_id}     | Admin only | 200    | Update a customization        |
| DELETE | /customizations/{customization_id}     | Admin only | 200    | Delete a customization        |

#### Key Features:
- **Bulk creation**: Assign the same customization to multiple laptops at once via `laptop_ids` array
- **Bulk creation by pattern**: Assign customization to all laptops matching a `target_pattern` in their `model_code` (e.g., "m5-max")
- Pre-validates all laptop IDs exist before inserting
- Partial update support (PATCH with `exclude_unset`)
- Bidirectional relationship: Laptop ↔ LaptopCustomization

---

### 5. **Web Scraping Pipeline** (`app/scraper/`)

Automated web scraping system using Playwright for extracting laptop specs from manufacturer websites. Supports single-URL scraping and brand-wide bulk scraping.

#### Files:
- `app/scraper/router.py` — Scraper API endpoints (feed-crawler, scrape-url, bulk-scrape, raw-laptop detail)
- `app/scraper/models.py` — ScrapeTarget (`laptop_scrape_urls`) and RawScrapLaptop (`raw_scrap_laptops`) models
- `app/scraper/apple_scraper.py` — Apple-specific crawler and spec extractor (async, offloaded to worker thread)
- `app/scraper/asus_scraper.py` — Asus/ROG-specific `__NUXT__` JSON state extractor (variant-aware, async)
- `app/scraper/bulk_scraper.py` — Bulk scraping orchestration (processes all pending URLs for a brand)
- `app/scraper/playwright_utils.py` — Thread runner for async Playwright to solve Windows `SelectorEventLoop` limitations

#### ScrapeTarget Model:
```
- **id**: UUID (primary key)
- **url**: str (unique, indexed)
- **brand_id**: UUID (foreign key → laptop_brands.id)
- **last_scraped_at**: Optional[datetime]
- **is_active**: bool (default: True)
- **created_at**: datetime (auto-timestamp)
```

#### RawScrapLaptop Model:
```
- **id**: UUID (primary key)
- **source_url**: str (unique, indexed)
- **brand_id**: UUID (foreign key → laptop_brands.id)
- **raw_product_name**: str
- **raw_prices**: List[str] (JSONB)
- **image_urls**: List[str] (JSONB)
- **raw_specs_dump**: Dict[str, Any] (JSONB)
- **processing_status**: str (default: "pending" — states: pending/processing/completed/failed)
- **created_at**: datetime (auto-timestamp)
```

#### Endpoints Implemented:

| Method | Endpoint                          | Auth       | Status  | Purpose                                      |
| ------ | --------------------------------- | ---------- | ------- | -------------------------------------------- |
| POST   | /scraper/feed-crawler             | Admin only | 200     | Crawl site for spec page links               |
| POST   | /scraper/scrape-url               | Admin only | 200     | Scrape a single URL                          |
| POST   | /scraper/bulk-scrape              | Admin only | 200/207 | Bulk scrape all pending URLs for a brand     |
| GET    | /scraper/raw-laptop/{raw_laptop_id} | Admin only | 200   | Get full details of a single raw scraped record |

#### Scraping Workflow:

1. **Feed Crawler** (`/scraper/feed-crawler`):
   - Accepts a start URL and brand_id
   - Uses Playwright (headless Chromium) to find "Learn More" links
   - Filters for target links (e.g., Mac specs or Asus product details)
   - Blacklists irrelevant pages (accessories, displays, etc.)
   - Stores unique URLs in `laptop_scrape_urls` table
   - Deduplication: tracks newly added vs. already-in-queue counts
   - Supports Apple and Asus brands

2. **Scrape URL** (`/scraper/scrape-url`):
   - Accepts a URL and brand_id
   - Prevents duplicate scraping (checks existing RawScrapLaptop via `source_url LIKE`)
   - Dispatches to brand-specific extractor (Apple or Asus)
   - Saves raw data to `raw_scrap_laptops` staging table with `processing_status = "pending"`
   - Stamps `last_scraped_at` on ScrapeTarget regardless of outcome
   - Supports multi-variant pages (saves one RawScrapLaptop per variant with `?v=N` suffix)

3. **Bulk Scrape** (`/scraper/bulk-scrape`):
   - Accepts a brand_id in request body
   - Queries all ScrapeTarget rows where `is_active=True` and `last_scraped_at IS NULL`
   - Iterates each pending URL and dispatches to the appropriate brand scraper
   - Skips URLs already scraped (duplicate detection via `source_url LIKE`)
   - Stamps `last_scraped_at` regardless of success/failure
   - Saves successful variants to `raw_scrap_laptops` with `processing_status = "pending"`
   - Returns HTTP 200 on full success, HTTP 207 Multi-Status on partial failures
   - Writes timestamped failure logs to `logs/scraper/` directory on any errors
   - Response payload: `{ brand, total_pending, processed, succeeded, failed, skipped, log_file, results[] }`

4. **Raw Laptop Detail** (`/scraper/raw-laptop/{raw_laptop_id}`):
   - Retrieves full details of a single RawScrapLaptop record by UUID
   - Returns all fields: raw_specs_dump, image_urls, raw_prices, processing_status, created_at

#### Apple Scraper Implementation (`apple_scraper.py`):
- **`crawl_apple_specs_links()`**: Discovers spec page URLs from Apple's Mac product pages
- **`_extract_apple_specs()`**: Extracts product name, product images, and raw spec text from `.techspecs-section` elements. Image hygiene: no longer collects the `og:image` meta tag, and drops any `<img>` URL containing `icon`, `logo`, `/meta/`, or `_og.` (social-preview cards are not product shots). The raw row keeps the full family image set (e.g. both 13″ and 15″) — the per-variant size split happens in the AI processor, where `display_size_inch` exists
- **`scrape_official_website()`**: Orchestrates the full scrape-and-extract flow per URL

#### Asus Scraper Implementation (`asus_scraper.py`) — Dual Extraction Paths:

**ROG Pages** (`rog.asus.com`):
- Navigates to `/spec/` subpage and extracts from `window.__NUXT__.state.Spec.spec`
- Reads variant array directly from Nuxt.js state — zero CSS selectors needed
- Extracts per-variant: `specContent[]` → specs dict, `price` → formatted `RM` price, `mktName` + `skuName` → product name, `skuImg` → hero image
- Returns `list[dict]` — one entry per variant (SKU), with `?v=N` suffix for multi-variant pages

**Standard ASUS Pages** (`www.asus.com`):
- Navigates to `/techspec/` subpage and extracts from `window.__NUXT__.state.PDPage`
- Tries variant data from keys: `PDTechSpecM2List`, `PDTechSpec`, `PDTechSpecM2` → `TechSpec[]`
- Falls back to `SpecList` if no `TechSpec` list found
- Builds a `price_map` from multiple price list keys (`PDPriceList`, `modelPrice`, `modelSkuPrice`) with fallback logic
- Extracts per-variant: `SpecList` → specs dict, price by `ProductId` lookup → fallback to single page price, `ImageLink` → image URL
- Returns `list[dict]` — variant-aware with `?v=N` suffix

#### Bulk Scraper Implementation (`bulk_scraper.py`):
- **`run_bulk_scrape(brand_id, session)`**: Main orchestration function
  - Validates brand exists, queries all pending ScrapeTargets
  - Dispatches to correct brand scraper via `_dispatch_scraper()`
  - Tracks results in `BulkScrapeReport` dataclass (brand_name, total_pending, processed, succeeded, failed, skipped, log_file, results)
  - Per-URL result tracked in `UrlResult` dataclass (url, status, error)
- **`_write_failure_log()`**: Writes timestamped plain-text failure log to `logs/scraper/` directory
- **`_stamp_last_scraped()`**: Updates `last_scraped_at` on ScrapeTarget and commits

#### Windows event loop compatibility (`playwright_utils.py`):
- Offloads async Playwright tasks to an isolated worker thread with a dedicated event loop (using `ProactorEventLoop` on Windows) to prevent event loop mismatch errors under Uvicorn's default Windows `SelectorEventLoop`.

---

### 6. **AI Data Processor** (`app/processor/`)

LLM-powered engine that transforms raw scraped laptop data into structured, normalized database records.

#### Files:
- `app/processor/router.py` — Processor API endpoints
- `app/processor/engine.py` — LLM extraction engine (Google Gemini via LangChain)
- `app/processor/schemas.py` — ExtractedLaptopVariant (94 fields), ExtractedLaptopFamily

#### Endpoints Implemented:

| Method | Endpoint                      | Auth       | Status | Purpose                          |
| ------ | ----------------------------- | ---------- | ------ | -------------------------------- |
| POST   | /processor/process/{raw_id}   | Admin only | 200    | Process single raw laptop        |
| POST   | /processor/process-pending    | Admin only | 200    | Batch process all pending items  |
| POST   | /processor/categorize-untagged | Admin only | 200   | Backfill category tags for laptops with none |

#### AI Processing Workflow:

1. Fetches raw data from `raw_scrap_laptops` by ID
2. Resolves brand name from `laptop_brands`
3. Constructs a detailed system prompt with extraction rules:
   - **Combinatorial Safety rules**: base processors first, configuration upgrades mapping.
   - **Price Matrix Isolation**: only assign explicit prices, output `0.0` for upgrades with unknown prices to prevent hallucination.
   - **SKU Code Generation rules**: standardized format (Apple and PC formats).
   - Apple-specific inference (macOS, Apple GPU/CPU, ai_ready=true)
   - Catches unmapped specs in `unmapped_specs` field
4. Invokes Google Gemini (`gemini-3.5-flash`, temperature=0) with structured output
5. Maps each `ExtractedLaptopVariant` to a `Laptop` DB record. **Per-variant image filtering** (`_filter_variant_images`): one raw scrape can cover a whole family (MacBook Air 13″ + 15″ share a specs page), so each variant only keeps image URLs whose `NN-inch`/`NN_inch` path token matches its own `display_size_inch` (13.6 → `13`); size-agnostic URLs (shared shots) are kept, and `/meta/…_og.png` social-preview cards still in older raw rows are dropped. Re-processing a raw row overwrites `image_urls` with the filtered set
6. Handles duplicate SKUs via `IntegrityError` catch + rollback
7. **Category tagging**: each variant's `categories` field (1–3 use-case tags, judged from hardware) is matched case-insensitively against the `categories` table — existing tags are reused (active ones are injected into the prompt as `[AVAILABLE CATEGORIES]`), unknown tags are auto-created, links written to `laptop_categories` (additive — manual tags never removed). `POST /processor/categorize-untagged` backfills laptops with zero links from their stored specs (one Gemma call each, 5 s throttle, re-runnable)
8. Updates `processing_status` on the raw record (sets to 'completed')

#### ExtractedLaptopVariant Schema (94 fields):
Mirrors the 9-part Laptop model with extensive AI extraction instructions in each field's `description`. Includes an additional `unmapped_specs` dict for catch-all data that doesn't map to defined fields.

---

### 7. **Benchmark Scoring System** (`app/benchmark/`)

CPU and GPU benchmark data management with automated scraping from PassMark.

#### Files:
- `app/benchmark/model.py` — CPUBenchmark (table), GPUBenchmark (table), Create/Update/Read schemas
- `app/benchmark/router.py` — Benchmark CRUD endpoints + scraper triggers
- `app/benchmark/cpu_scraper.py` — PassMark CPU list scraper (Playwright → PostgreSQL upsert)
- `app/benchmark/gpu_scraper.py` — PassMark GPU/Video Card list scraper (Playwright → PostgreSQL upsert)

#### CPUBenchmark Model:
```
- id: UUID (primary key)
- cpu_name: str (unique, indexed)
- cpu_mark: int (default: 0)
```

#### GPUBenchmark Model:
```
- id: UUID (primary key)
- gpu_name: str (unique, indexed)
- gpu_mark: int (default: 0)
```

#### Endpoints Implemented:

| Method | Endpoint              | Auth       | Status | Purpose                            |
| ------ | --------------------- | ---------- | ------ | ---------------------------------- |
| POST   | /benchmarks/cpu       | Admin only | 201    | Create CPU benchmark entry         |
| GET    | /benchmarks/cpu       | None       | 200    | List CPU benchmarks (paginated)    |
| GET    | /benchmarks/cpu/{id}  | None       | 200    | Get specific CPU benchmark         |
| PATCH  | /benchmarks/cpu/{id}  | Admin only | 200    | Update CPU benchmark               |
| DELETE | /benchmarks/cpu/{id}  | Admin only | 204    | Delete CPU benchmark               |
| POST   | /benchmarks/scrape/cpu| Admin only | 200    | Trigger PassMark CPU scraper       |
| POST   | /benchmarks/gpu       | Admin only | 201    | Create GPU benchmark entry         |
| GET    | /benchmarks/gpu       | None       | 200    | List GPU benchmarks (paginated)    |
| GET    | /benchmarks/gpu/{id}  | None       | 200    | Get specific GPU benchmark         |
| PATCH  | /benchmarks/gpu/{id}  | Admin only | 200    | Update GPU benchmark               |
| DELETE | /benchmarks/gpu/{id}  | Admin only | 204    | Delete GPU benchmark               |
| POST   | /benchmarks/scrape/gpu| Admin only | 200    | Trigger PassMark GPU scraper       |

#### Key Features:
- **PassMark Scraping**: Headless Playwright scraper targets `cpubenchmark.net/cpu_list.php` and `videocardbenchmark.net/gpu_list.php`
- **PostgreSQL Upsert**: Uses `INSERT ... ON CONFLICT DO UPDATE` for idempotent data sync — updates existing scores, inserts new entries
- **Background Execution**: Scraper pipelines dispatched via FastAPI `BackgroundTasks` to prevent API timeout
- **Duplicate Protection**: Unique index on `cpu_name` / `gpu_name` prevents duplicate entries (409 Conflict on manual create)

---

## 📊 Data Models Summary

### Database Tables

| Table                    | Primary Key | Key Relations                     |
| ------------------------ | ----------- | --------------------------------- |
| `users`                  | UUID        | → laptop_user_preference (1:1)    |
| `laptop_user_preference` | UUID        | → users (FK: user_id)             |
| `laptops`                | UUID        | → laptop_brands (FK: brand_id), → laptop_customizations (1:N), → laptop_embeddings (1:1), → laptop_price_history (1:N) |
| `laptop_brands`          | UUID        | ← laptops, ← raw_scrap_laptops, ← laptop_scrape_urls |
| `laptop_customizations`  | UUID        | → laptops (FK: laptop_id), → categories (FK: category_id) |
| `raw_scrap_laptops`      | UUID        | → laptop_brands (FK: brand_id)    |
| `laptop_scrape_urls`     | UUID        | → laptop_brands (FK: brand_id)    |
| `laptop_embeddings`      | UUID        | → laptops (FK: laptop_id, unique) — 768-dim pgvector |
| `laptop_price_history`   | UUID        | → laptops (FK: laptop_id) — price snapshots on create + PUT change |
| `cpu_benchmarks`         | UUID        | Standalone (unique cpu_name)      |
| `gpu_benchmarks`         | UUID        | Standalone (unique gpu_name)      |
| `product_types`          | UUID        | ← questionnaire_questions (FK: product_type_id) |
| `categories`             | UUID        | ← laptop_categories (M:N with laptops), ← laptop_customizations (FK: category_id) |
| `laptop_categories`      | (laptop_id, category_id) composite | → laptops, → categories — junction table |
| `questionnaire_questions`| UUID        | → product_types (FK: product_type_id) |

### Request/Response Schemas

- **UserRegisterRequest**: username (no `@`, non-blank validator), email, password (with complexity validator)
- **GoogleLoginRequest**: id_token (Google ID token JWT from Google Identity Services)
- **UserRead**: User profile for read operations (excludes password)
- **UserProfile**: birthday, gender (validated enum), occupation
- **UserPreferences**: All preference fields with validators
- **Token**: access_token, token_type
- **ForgotPasswordRequest**: email (EmailStr)
- **ResetPasswordRequest**: token, new_password
- **LaptopCreate/LaptopRead/LaptopUpdate**: Full 9-part laptop spec schemas
- **BrandCreate/BrandRead/BrandUpdate**: Brand management schemas
- **CustomizationBulkCreate/CustomizationRead/CustomizationUpdate**: Customization schemas (`category_id` FK, not a free string)
- **CustomizationBulkCreateByPattern**: Pattern-based bulk customization creation
- **ProductTypeCreate/ProductTypeRead/ProductTypeUpdate**: Product type taxonomy schemas
- **CategoryCreate/CategoryRead/CategoryUpdate**: Category (tag) taxonomy schemas
- **QuestionnaireQuestionRead**: Questionnaire catalog read schema
- **ExtractedLaptopVariant/ExtractedLaptopFamily**: AI extraction output schemas
- **CPUBenchmarkCreate/CPUBenchmarkRead/CPUBenchmarkUpdate**: CPU benchmark schemas
- **GPUBenchmarkCreate/GPUBenchmarkRead/GPUBenchmarkUpdate**: GPU benchmark schemas
- **ScraperRequest**: url, brand_id
- **CrawlerQueueRequest**: start_url, brand_id
- **BulkScrapeRequest**: brand_id
- **BulkScrapeReport**: brand_name, total_pending, processed, succeeded, failed, skipped, log_file, results (dataclass)
- **UrlResult**: url, status, error (dataclass)

---

## 🔒 Security Features

### Password Management
- Hashing: bcrypt algorithm with salt (via passlib)
- Complexity requirements: Min 8 chars, 1 uppercase, 1 lowercase, 1 number
- Nullable for Google-created accounts (no local password); password login guards `password IS NULL`

### Google Sign-In
- ID-token verification flow (no server-side OAuth redirect): `google-auth` checks signature, expiry, issuer, and audience (`GOOGLE_OAUTH_CLIENT_ID`)
- Requires `email_verified` claim from Google; accounts matched by stable `provider_sub` first, then linked by email
- Google-created/linked accounts are auto-verified (provider already verified the email)

### Email Verification
- JWT tokens with configurable expiration (default: 1 hour)
- Scoped tokens (`scope: "email_verification"`)
- Prevents login without verified email

### JWT Authentication
- Access tokens with configurable expiration (default: 10080 minutes / 7 days)
- OAuth2 PasswordBearer scheme (`tokenUrl="auth/login"`)
- Token validation in protected endpoints
- Password reset tokens with 15-minute expiry (`scope: "password_reset"`)

### Role-Based Access Control
- `get_current_user`: Extracts and validates user from JWT token
- `get_current_admin`: Requires `role == "admin"`, returns 403 Forbidden otherwise
- Admin-protected endpoints: brand mutations, customizations, scraper, processor, benchmarks (write/scrape), raw scrap data listing

### Data Validation
- Email format validation (EmailStr from pydantic)
- Username validation (non-blank, no `@` — can't shadow an email in the username-or-email login lookup)
- Gender enum enforcement (Male, Female, Other)
- Tech-savviness enum enforcement (Very tech-savvy, Somewhat tech-savvy, Not very tech-savvy)
- Partial update support with `exclude_unset`
- Brand name uniqueness enforcement
- Benchmark name uniqueness enforcement

---

## 📦 Database Migrations

| Migration ID   | Description                                    | Date       | Status      |
| -------------- | ---------------------------------------------- | ---------- | ----------- |
| 09e0b89d409b   | Init laptop and user tables                    | 2026-05-24 | ✅ Complete |
| b822221532ba   | Add is_verified to user                        | 2026-05-30 | ✅ Complete |
| 2c9080e9c975   | Add JSONB preferences to users                 | 2026-05-31 | ✅ Complete |
| 3a1f2e3d4c5b   | Migrate preferences to dedicated table         | 2026-05-31 | ✅ Complete |
| c7d3e2f1a4b9   | Add user profile and tech_savviness            | 2026-05-31 | ✅ Complete |
| 2be2003ece47   | Add UUID id to laptops                         | 2026-06-02 | ✅ Complete |
| eee71a25ba33   | Add raw_scrap_laptops staging table            | 2026-06-07 | ✅ Complete |
| ddbc0e4de136   | Change image_url to image_urls array           | 2026-06-09 | ✅ Complete |
| 3ebc95cb5885   | Replace raw_price_rm with raw_prices (array)   | 2026-06-09 | ✅ Complete |
| 65e8c344a0e4   | Add laptop_scrape_urls table                   | 2026-06-09 | ✅ Complete |
| e1a2b3c4d5e6   | Create laptop_brands table                     | 2026-06-10 | ✅ Complete |
| f3d8e2c1b5a9   | Convert brand string to UUID foreign key       | 2026-06-10 | ✅ Complete |
| e482248b8968   | Merge two heads                                | 2026-06-10 | ✅ Complete |
| f3fcaa26fbe0   | Add icons_url to brands + brand_id to laptops  | 2026-06-10 | ✅ Complete |
| e67b02a435fd   | Change laptop image_url data type              | 2026-06-10 | ✅ Complete |
| 1ae7fc1d26e1   | Update laptop table (add processor_model, gpu_model, drop benchmarks) | 2026-06-11 | ✅ Complete |
| 82a7e9d6fc2b   | Major laptop table update (30+ new columns)    | 2026-06-14 | ✅ Complete |
| 89a86e2d5749   | Create laptop_customizations table             | 2026-06-15 | ✅ Complete |
| 95483e847e0e   | Create cpu_benchmarks and gpu_benchmarks tables| 2026-06-16 | ✅ Complete |
| (auto)         | Create laptop_embeddings table (Vector 768)    | 2026-06-20 | ✅ Complete |
| (auto)         | Create laptop_price_history table              | 2026-06-20 | ✅ Complete |
| 74117c66e44c   | Add conversations, messages, conversation_laptops | 2026-06-29 | ✅ Complete |
| 851c59e8102d   | Add pipeline_eval_logs                         | 2026-06-29 | ✅ Complete |
| fe5716d7dbf4   | Add youtube review ingestion tables            | 2026-07-01 | ✅ Complete |
| ffb4429867dd   | Add taxonomy (product_types, categories), questionnaire_questions, laptop_customizations.category→category_id FK, laptop_user_preference.budget→JSON range | 2026-07-04 | ✅ Complete |
| b3d91a4c72e0   | Google login fields on users: password→nullable, auth_provider (default 'local'), provider_sub (unique index) | 2026-07-14 | ✅ Complete |
| c8f24d1e9a37   | Add MULTIPLE_CHOICE to questiontype Postgres enum | 2026-07-14 | ✅ Complete |
| e5a7c093b1d4   | Add user_avatars table (bytea, 1:1 users, unique user_id index) | 2026-07-14 | ✅ Complete |
| a91f3c5d80e2   | Add laptop_pick_scores table (unique laptop_id + use_case, JSON breakdown/flags) | 2026-07-16 | ✅ Complete |

---

### 8. **PickScore v2 Engine** (`app/pickscore/`)

Fully deterministic, product-agnostic scoring engine. No LLM involvement. Produces a structured breakdown consumed by the LLM for conversational explanations.

#### Files:
- `app/pickscore/engine.py` — 8 factor scoring functions + 3-layer weighting pipeline
- `app/pickscore/schemas.py` — `ScorableProduct` dataclass, `PickScoreResponse`, `BatchPickScoreResponse`, `FactorBreakdown`
- `app/pickscore/benchmark_service.py` — RapidFuzz fuzzy matching against benchmark tuples; module-level 5-min cache; confidence threshold 0.6
- `app/pickscore/ranges_cache.py` — Generic TTL cache for min/max ranges

#### Laptop Adapter (`app/laptops/`):
- `app/laptops/pickscore_adapter.py` — `laptop_to_scorable()` + `get_laptop_ranges()`. Ranges computed from catalog laptops only (not global PassMark table) to keep normalization within laptop-class hardware.
- `app/laptops/pickscore_router.py` — `POST /laptops/calculate-score` and `POST /laptops/calculate-score/batch`

#### 8 Scoring Factors (all 0–100):

| Factor | Logic |
|---|---|
| `price` | Personalized: 100 if ≤ budget, else decay. General: inverse min-max |
| `cpu` | `normalize(cpu_mark)` via RapidFuzz benchmark match |
| `gpu` | `normalize(gpu_mark)`; Apple always proxies via CPU score (ARM SoC — no PassMark GPU data) |
| `ram_storage` | `ram×0.6 + storage×0.4`; HDD gets −15 penalty |
| `portability` | Inverse normalize on `weight_kg` |
| `battery` | Normalize on `battery_wh` |
| `screen_size` | Bucket distance scoring |
| `brand` | 100 if in `brand_preferences`, else 50 |

#### 3-Layer Weighting: `baseWeight × purposeModifier × portabilityModifier`

Two modes: **Personalized** (uses `LaptopUserPreference`) and **General** (DEFAULT_PRIORITY N-i rule). General mode accepts a `priority_override` base-weight profile (ignored in personalized mode) — used by the precomputed use-case scores (see §17).

---

### 9. **Embeddings Module** (`app/embeddings/`)

Generates and stores 768-dim vector embeddings for all laptops using Gemini `models/gemini-embedding-2` (moved back to Gemini on 2026-07-17 after a one-day OpenRouter nemotron-embed detour — **re-run generate-all + re-process review chunks after the switch**).

#### Files:
- `app/embeddings/service.py` — `build_laptop_embedding_text()`, `embed_text()`, `upsert_laptop_embedding()`, `generate_all_laptop_embeddings()`
- `app/embeddings/router.py` — Admin-only generate-all / generate-single / status endpoints

#### Key Design:
- Builds natural-language document per laptop (not raw JSON) for better semantic retrieval
- `output_dimensionality=768` to match existing `Vector(768)` column — pins the model's larger default down, no schema migration needed
- `embed_text()` is the single embedding entry point for the whole app (hybrid search, RAG retrieval, review chunks, recommendation)
- **Changing the embedding model changes the vector space** — always re-run generate-all afterwards, and recalibrate `RELEVANCE_THRESHOLD` (see §12)

#### Endpoints:

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | /embeddings/generate-all | Admin | Embed all laptops (background task) |
| POST | /embeddings/generate/{id} | Admin | Embed single laptop |
| GET | /embeddings/status | Admin | Count embedded vs total laptops |

---

### 10. **Hybrid Search & Price History** (`app/laptops/laptop_router.py`)

#### Hybrid Search (`POST /laptops/hybrid-search`):
- Embeds user query with the same embedding model as the documents (via `embed_text()`)
- Joins `laptops ↔ laptop_embeddings ↔ laptop_brands` ordered by pgvector cosine distance (`<=>`)
- Optional hard filters: `budget_max`, `brand`
- Returns `LaptopSearchResult` with `similarity_score = 1 − distance`

#### Price History:
- `laptop_price_history` table captures `price_rm` on create and on every PUT price change
- `GET /laptops/{id}/price-history` returns ordered price series

---

### 11. **LLM Recommendation Layer** (`app/recommendation/`)

Full pipeline: hybrid search → PickScore → Gemini LLM explanations.

#### Files:
- `app/recommendation/schemas.py` — `RecommendationRequest`, `RecommendationResponse`, `RecommendedLaptop`, internal `_LLMOutput`
- `app/recommendation/service.py` — Orchestration: hybrid search → batch PickScore → re-rank → Gemini
- `app/recommendation/router.py` — `POST /recommendations/laptops`

#### Pipeline:
1. Auth gate + require `LaptopUserPreference` (400 if missing)
2. Hybrid search → candidate pool (default 15)
3. Batch PickScore (personalized, shared data fetched once)
4. Re-rank by PickScore → top_k (default **3**)
5. Gemini `gemini-1.5-flash` with `with_structured_output` → per-laptop explanation (2-3 sentences) + overall summary (3-4 sentences)
6. Language adapts to `tech_savviness` (3 levels)

#### Endpoint:

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | /recommendations/laptops | Bearer Token | Full recommendation pipeline |

---

### 12. **RAG pipeline + conversation threads** (`app/rag/`, renamed from `app/conversations/`)

**Consolidated into the agent (see §13) per `CRS_Agent_Consolidation_Spec.md`.** The 5-module retrieve → rerank → relax → gate → evaluate pipeline is retained as internal library logic — it is no longer driven by a standalone chat endpoint or intent-detection call. It is now consumed directly by the `search_laptops` agent tool (`app/agent/tools/search_laptops.py`). The folder also still owns conversation-thread persistence (`Conversation`/`Message`/`ConversationLaptop`), so the name covers both the RAG pipeline and the conversation-thread models it feeds.

#### Files:
- `retrieval.py` — Module 1: pgvector cosine similarity search (top-50 recall), 5-min in-process query cache, relational fallback on embedding API timeout
- `reranker.py` — Module 2: constraint-aware reranking via penalty multipliers (budget, weight) + bonuses (purpose, brand). Decoupled `UserConstraints` dataclass independent of ORM
- `relaxation.py` — Module 3: stepwise constraint relaxation when 0 viable candidates — weight first (+0.2kg × 2 steps), then budget (+RM 500 × 3 steps), brand never auto-relaxed
- `gating.py` — Module 4: relevance threshold (calibrated `0.53` — **embedding-model-specific**; spot-checked on the re-embedded catalog for gemini-embedding-2, where relevant tops measure 0.537–0.732 vs irrelevant 0.418–0.546) intercepts low-confidence results; `_detect_bottleneck()` routes targeted clarification questions (budget / weight / general). `relaxation.py`'s `_MIN_VIABLE_SCORE` = 0.33 (62.5% of the gate)
- `evaluation.py` — Module 5: NDCG@10 offline evaluation + `log_pipeline_result()` writes to `pipeline_eval_logs` table and `logs/eval/pipeline_trace.jsonl` on every live `search_laptops` call
- `models.py` — DB tables: `Conversation`, `Message`, `ConversationLaptop`, `PipelineEvalLog` — retained, now populated by `POST /agent/chat` (message history + shortlist pool) instead of the old CRS chat flow
- `service.py` — Conversation-thread CRUD only (`create_conversation`, `list_conversations`, `get_conversation`, `delete_conversation`, `_generate_title`); reused by `app/agent/router.py`
- `router.py` — 4 FastAPI endpoints (all auth-required): create/list/get/delete a conversation thread. **`POST /{id}/chat` has been removed** — chat now goes through `POST /agent/chat` (see §13)

#### Reranking Formula:
```
final_score = similarity_score × penalty_multiplier + bonus
```

| Penalty/Bonus | Trigger | Value |
|---|---|---|
| Budget soft penalty | 0–10% over budget | `max(0.5, 1 − over_ratio × 2)` |
| Budget medium penalty | 10–30% over budget | `max(0.3, 1 − over_ratio × 1.5)` |
| Budget hard penalty | >30% over budget | `0.1` |
| Weight soft penalty | ≤20% over weight_limit | `0.7` multiplier |
| Weight hard penalty | >20% over weight_limit | `0.2` multiplier |
| Purpose bonus | GPU/CPU signals match purpose | `+0.04` per match, capped at `+0.08` |
| Brand bonus | Brand in preferences | `+0.05` |

#### Live Quality Logging (`pipeline_eval_logs`):
Every live `search_laptops` tool call logs: `gate_status`, `top_score`, `relaxed_field`, `bottleneck`, `candidate_count`, `result_laptop_ids`, `user_id`, `conversation_id`.

---

### 13. **ReAct Agent** (`app/agent/`, via `langchain.agents.create_agent`)

Sole conversational entry point (`POST /agent/chat`). A ReAct agent that reasons across four tools, absorbing the CRS pipeline's precision logic (retrieve → rerank → relax → gate) into `search_laptops` instead of running it as a fixed, separate pipeline. The agent — not a hardcoded step order — decides when to search, when to ask a clarifying question, and when it has enough to recommend.

#### Files

- `app/agent/tools/search_laptops.py` — `search_laptops(user_query, budget_max, brand, purpose, top_k=10)`: runs the CRS `retrieve_candidates` → `rerank` → (`relax_and_retry` if 0 viable candidates) → `relevance_gate` pipeline. Returns `{results, confidence: "high"|"low", bottleneck, message, relaxation_notice}`. Each result carries **`pick_score`** (0–100, general-mode PickScore batch-computed on the gated top-k — ranges + benchmark tuples fetched once, same pattern as the recommendation service) plus a compact `pick_score_top_factors` summary (top-3 factors only; the full 8-factor breakdown would bloat LLM context and overflow eval-judge truncation). PickScore failure is non-fatal — results go out unscored. On low confidence, returns no laptops but a targeted clarification message — the agent (not the tool) decides how to use it. Logs every call to `pipeline_eval_logs` via `evaluation.log_pipeline_result()`.
- `app/agent/tools/laptop_tools.py` — `calculate_custom_apple_price` (base price + customization add-ons), `get_review_evidence` (pgvector cosine search over review chunks, returns top-3 with YouTube timestamp links)
- `app/agent/tools/market_price.py` — `search_malaysian_market_price(product_name, model_code)`: two-layer price lookup — (1) catalog layer from own DB (official `price_rm` + last 5 price-history snapshots; `model_code` exact match, else RapidFuzz fuzzy match on `product_name`), and (2) live Malaysian retail listings via SerpApi Google Shopping (`SERP_API_KEY`, optional; free tier ~100 searches/month) with accessory-keyword blocklist, RM 800 price floor, max 2 listings per store, and a 6-hour in-process cache. Shopee/Lazada search links always included as last-resort fallback
- `app/agent/graph.py` — `run_agent(message, history, conv_laptops, session)`: reconstructs conversation state from the `messages` table (last 12 turns) + the current `conversation_laptops` shortlist each call (no LangGraph checkpointer — state lives in Postgres via the existing conversation tables), builds the agent via `langchain.agents.create_agent` with the LLM from `build_agent_llm()` — `AGENT_MODEL = "gemma-4-31b-it"` via `ChatGoogleGenerativeAI` (`GEMINI_API_KEY`; back on Gemma as of 2026-07-17 after a one-day OpenRouter nemotron detour; the factory is the single source of truth, imported by the eval harness so evals always measure production config), and extracts the latest `search_laptops` tool result from the run so the caller can update the shortlist pool. The reply passes through `_content_to_text()` — some models return content as a list of typed blocks (`thinking` + `text`) instead of a plain string, which crashed the `messages` insert (`psycopg2 can't adapt type 'dict'`); text blocks are joined, thinking blocks dropped (fallback-only so the reply is never empty)
- `app/agent/router.py` — `POST /agent/chat` (auth required); accepts optional `conversation_id` (auto-creates a new conversation if omitted); persists user/assistant `Message` rows and replaces the `conversation_laptops` pool (with `pick_score` + `similarity_score` snapshots) when `search_laptops` returns high confidence; returns 503 if the agent errors. The response includes a structured **`laptops` field** (`AgentLaptopCard`: laptop_id, product_name, price_rm, pick_score, similarity_score) for frontend score badges — fresh search results when a search ran this turn, otherwise the persisted pool joined to `laptops` (similarity DESC, NULLS LAST) so follow-up turns keep their cards

#### Architecture

```text
POST /agent/chat  (conversation_id optional — auto-creates if omitted)
  └── run_agent(message, history, conv_laptops, session)
        ├── search_laptops               → retrieve → rerank → relax → gate + general-mode PickScore on top-k
        ├── calculate_custom_apple_price → PostgreSQL (base price + sum of selected add-ons)
        ├── get_review_evidence          → pgvector cosine search on laptop_review_chunks
        └── search_malaysian_market_price → catalog price + history, live listings (SerpApi Google Shopping)
```

No HTTP round-trips — all tools query the DB directly. The agent decides which tool(s) to call and in what order based on the user's message and replayed history; there is no separate intent-classification call.

#### Endpoint

| Method | Endpoint    | Auth         | Purpose                                                        |
| ------ | ----------- | ------------ | -------------------------------------------------------------- |
| POST   | /agent/chat | Bearer Token | ReAct agent — search (with PickScore) + pricing + review evidence + market prices; returns text reply + structured `laptops` shortlist |

---

### 14. **YouTube Review Ingestion Pipeline** (`app/reviews/`)

End-to-end pipeline that discovers YouTube laptop review videos, fetches transcripts, matches them to catalog laptops, chunks and embeds the content for RAG retrieval.

#### Files

- `app/reviews/models.py` — 4 DB tables: `YoutubeChannel`, `RawYoutubeReview`, `LaptopReviewChunk`, `LaptopReviewSummary`; schemas: `YoutubeChannelCreate` (URL-based), `YoutubeChannelUpdate`, `RawYoutubeReviewRead`, `ManualMatchRequest`
- `app/reviews/discovery.py` — `resolve_channel_from_url()` (parses 4 URL formats → `channels.list` API, 1 quota unit); `discover_videos()` (YouTube `search.list`, 100 quota units/channel, top 5 per channel)
- `app/reviews/transcript.py` — `fetch_transcript()`: `YouTubeTranscriptApi().fetch(video_id)` — v1.x instance API; returns `[{text, start, duration}]` or None if subtitles unavailable
- `app/reviews/matcher.py` — `match_laptop()`: RapidFuzz `token_set_ratio` against compact match keys (`_build_match_key` strips `-inch`/RAM/storage, extracts chip from parens); threshold 73
- `app/reviews/processor.py` — `process_raw_review()`: 45-second chunk windows → Gemini summary + sentiment tag → embed via the central `embed_text()` (`app/embeddings/service.py`) → save `LaptopReviewChunk` rows; 4-second delay between Gemini calls
- `app/reviews/aggregator.py` — `aggregate_for_laptop()`: top-5 distinct strengths + weaknesses from all chunks → upsert `LaptopReviewSummary`
- `app/reviews/service.py` — `ingest_for_laptop()`: full discovery → transcript → match pipeline; retries `rejected` rows (transient failures); skips `matched`/`pending`

#### DB Tables

- **`youtube_channels`** — `channel_id`, `channel_name`, `channel_img_url`, `trust_tier` (tier_1/tier_2), `active`
- **`raw_youtube_reviews`** — `video_id`, `video_title`, `raw_transcript` (JSONB), `matched_laptop_id`, `match_confidence`, `status` (pending/matched/rejected)
- **`laptop_review_chunks`** — `chunk_text` (LLM summary), `embedding` (Vector 768), `sentiment_tag` (strength/weakness/neutral), `timestamp_start/end_seconds`
- **`laptop_review_summary`** — `aggregated_strengths` + `aggregated_weaknesses` (JSONB top-5 each), `review_count`

#### Ingest Pipeline Stages

```text
1. discover_videos()      → YouTube search.list per channel (100 quota units each)
2. fetch_transcript()     → youtube-transcript-api v1.x (no quota cost)
3. match_laptop()         → RapidFuzz token_set_ratio against compact keys (threshold 73)
4. save RawYoutubeReview  → status: matched | pending | rejected
── manual step ──
5. process_raw_review()   → 45s chunks → Gemini summary + sentiment → embed → LaptopReviewChunk
6. aggregate_for_laptop() → roll up chunks → LaptopReviewSummary
```

#### Key Design Decisions

- `channel_url` input (not raw `channel_id`) — auto-resolved via `channels.list` for better admin UX
- `youtube_api_key: Optional[str] = None` in config — server starts without the key; endpoints raise 400 at call time if missing
- Retry logic: rejected videos are re-attempted on next ingest (may have failed due to transient errors); matched/pending are skipped
- Match key strips `-inch`/RAM/storage noise (never in video titles) and extracts chip from parens: `"Apple 14-inch MacBook Pro (M5, 16GB RAM...)"` → `"Apple 14 MacBook Pro M5"`
- `POST /reviews/rematch` re-runs auto-matching on all pending rows — useful after adjusting the threshold or match key logic

#### Endpoints

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | /reviews/channels | Admin | Resolve URL + create channel |
| GET | /reviews/channels | Admin | List channels |
| PATCH | /reviews/channels/{id} | Admin | Update channel |
| POST | /reviews/ingest/{laptop_id} | Admin | Full discovery + transcript + match pipeline |
| POST | /reviews/ingest-bulk | Admin | Bulk discovery across catalog — one search per laptop family (`?limit=` families/run, `?skip_covered=` skips already-matched families); quota-aware |
| GET | /reviews/raw | Admin | List raw reviews (filterable by status) |
| PATCH | /reviews/raw/{id}/match | Admin | Manual laptop pairing |
| POST | /reviews/rematch | Admin | Re-run auto-match on all pending reviews |
| POST | /reviews/process/{review_id} | Admin | Chunk + embed a matched review |
| POST | /reviews/aggregate/{laptop_id} | Admin | Recompute review summary |

---

### 15. **Taxonomy — Product Types & Categories** (`app/taxonomy/`)

Two small reference/lookup tables, both mirroring `app/laptops/brand_model.py` + `brand_router.py`'s exact CRUD shape (admin-only writes, public reads, 409 on duplicate name, 409 on delete if still referenced).

#### Files
- `app/taxonomy/product_type_model.py` — `ProductType` table (`id`, `name` unique, `is_active`, `created_at`) + Base/Create/Update/Read schemas. Scopes the questionnaire (§16) by product line — seeded with `"laptop"`; future product lines (phone, etc.) can be added without a new table.
- `app/taxonomy/product_type_router.py` — CRUD, deletion blocked (409) if any `questionnaire_questions` row still references it.
- `app/taxonomy/category_model.py` — `Category` table (`id`, `name` unique, `icon_url`, `is_active`, `created_at`) — marketing/use-case tags (Gaming, Business, Creator, etc.) for the frontend tag component. Two relationships: `laptops` (many-to-many via `laptop_categories` junction) and `customizations` (one-to-many, `LaptopCustomization.category_id`).
- `app/taxonomy/category_router.py` — CRUD, deletion blocked (409) if still tagged on any laptop or referenced by any customization.
- `app/laptops/laptop_category_model.py` — `LaptopCategory` junction table (`laptop_id`, `category_id` composite PK). Kept separate from the already-complex 200+ field `laptops` model — adding the `categories` relationship required no other changes to `Laptop`.

#### Endpoints

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | /product-types | Admin | Create product type |
| GET | /product-types | None | List product types |
| GET | /product-types/{id} | None | Get product type |
| PUT | /product-types/{id} | Admin | Update product type |
| DELETE | /product-types/{id} | Admin | Delete (409 if questions reference it) |
| POST | /categories | Admin | Create category (tag) |
| GET | /categories | None | List categories |
| GET | /categories/{id} | None | Get category |
| PUT | /categories/{id} | Admin | Update category |
| DELETE | /categories/{id} | Admin | Delete (409 if tagged/referenced) |

---

### 16. **Questionnaire Catalog** (`app/users/questionnaire_*`)

Backend catalog for the PickWise v1 6-step preference survey (Budget, Purpose, Priorities, Screen Size, Portability, Brand), so the frontend can render it dynamically instead of hardcoding questions/options. Catalog-only — no answer-submission endpoint; the frontend still writes final values via the existing `PUT /me/preferences`.

#### Files
- `app/users/questionnaire_model.py` — `QuestionnaireQuestion` table: `product_type_id` (FK), `step_order`, `question_text`, `question_type` (`single_choice` | `multiple_choice` | `ranking` — native Postgres enum `questiontype`), `target_field` (which `LaptopUserPreference` field the answer populates), `options` (JSON list of `{value, label}`, `null` for the brand question), `help_text`, `is_active`. Plus `Create`/`Update`/`Read` schemas (Read includes `is_active` + `created_at`).
- `app/users/questionnaire_router.py` — full CRUD mirroring the brand/category shape: public reads (`GET /questionnaire?product_type=laptop`, `include_inactive=true` for admin management views; `GET /questionnaire/{id}`), admin-only writes (POST/PUT/DELETE). Create/update validate the `product_type_id` exists (404) and return 409 if another **active** question already occupies the same `step_order` for that product type. Delete is hard — setting `is_active=false` is the preferred way to retire a question.

#### Seeded Questions

| step | target_field | type | notes |
|---|---|---|---|
| 1 | `budget` | single_choice | options are `{min,max}` RM ranges; open-ended "> RM 5000" → `{min:5000,max:null}` |
| 2 | `purpose` | single_choice | option values match `PURPOSE_MODIFIERS` keys in `app/pickscore/engine.py` exactly |
| 3 | `priorities` | ranking | option values match `DEFAULT_PRIORITY` factor keys (`price`, `cpu`, `gpu`, `portability`, `battery`, `brand`) |
| 4 | `screen_size` | single_choice | `13-14` / `15-16` / `17+` |
| 5 | `portability` | single_choice | option values match `PORTABILITY_MULTIPLIERS` keys (`Yes`/`Neutral`/`No`) |
| 6 | `brand_preferences` | single_choice | `options: null` — sourced dynamically from `GET /brands` rather than duplicating the brand list |

#### Endpoint

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| GET | /questionnaire | None | Active questions for a product type, ordered by step |

---

### 17. **Precomputed Use-Case PickScores** (`app/laptops/pickscore_general.py`)

Stores one general-mode PickScore per laptop × use case in the `laptop_pick_scores` table, so the frontend's use-case cards and rankings read precomputed rows instead of re-running benchmark fuzzy matching per page view.

#### Use-Case Weight Profiles (`USE_CASE_PRIORITIES`, same 1–10 scale as `DEFAULT_PRIORITY`):

| Slug | Emphasis |
|---|---|
| `office_study` | price 9, battery 8, portability 7 — cheap, mobile, all-day |
| `programming` | cpu 9, ram_storage 9, price 6, battery 6 — compile/IDE workloads; screen 3 over ultra-portability (4) |
| `gaming` | gpu 10, cpu 8, ram_storage 7; portability 1, battery 1 — portable desktop, raw performance only |
| `creative_work` | gpu 9, cpu 8, ram_storage 8, screen 4 — design/video/3D |
| `general_use` | price 9, cpu 7, ram_storage 7, portability 6, battery 6, gpu 2 — explicit all-rounder (replaced the N-i `DEFAULT_PRIORITY`, which overweighted GPU ~17% for daily use) |

#### Key Design:
- `laptop_pick_scores`: UUID pk, `laptop_id` FK, `use_case` slug (both indexed), `score`, full factor `breakdown` + `flags` as JSON, `updated_at`; unique on `(laptop_id, use_case)` — one row per laptop per use case is exactly the shape per-use-case ranking needs (`WHERE use_case = ? ORDER BY score DESC`)
- Use cases are a **fixed set tied to weight profiles in code** — deliberately *not* FK'd to the dynamic `categories` taxonomy table
- `generate_all_pick_scores()` fetches ranges + benchmark tuples once and upserts the whole catalog (245 laptops × 5 = 1,225 rows); deterministic, no LLM — regenerate after processor imports, benchmark refreshes, or profile changes
- **Gaming ranking proxy demotion**: Apple GPUs score via CPU proxy (no PassMark ARM data), which put M5 MacBooks above RTX 5090 machines; for the `gaming` use case only, rows flagged `gpu_score_is_proxy` sort after every real-benchmark laptop. `flags` are exposed per result for frontend badges

#### Endpoints:

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| GET | /laptops/{id}/pick-scores | None | All 5 use-case scores (+ breakdown) for one laptop; `?use_case=` filters |
| GET | /laptops/pick-scores/ranking | None | Top laptops for one use case (`?use_case=&limit=`), price breaks ties |
| POST | /laptops/pick-scores/generate-all | Admin only | Recompute + upsert all stored scores |

---

## 📋 Complete API Endpoints Summary

**All endpoints below are served under the `/api/v2` prefix** (e.g. `/auth/register` is actually `POST /api/v2/auth/register`), applied via `prefix="/api/v2"` on every `app.include_router(...)` call in `app/main.py`. The root health check (`GET /`) and the auto-generated `/docs`, `/redoc`, `/openapi.json` stay unprefixed. Tables below omit the prefix for brevity.

### Authentication (`/auth`)

| Method | Endpoint              | Auth              | Purpose                 |
| ------ | --------------------- | ----------------- | ----------------------- |
| POST   | /auth/register        | None              | Register new user       |
| GET    | /auth/verify-email    | Query param       | Verify email            |
| POST   | /auth/login           | OAuth2 form       | Get JWT token (username or email) |
| POST   | /auth/google          | Google ID token   | Sign in with Google     |
| PUT    | /auth/me/avatar       | Bearer Token      | Upload/replace avatar   |
| GET    | /auth/avatar/{user_id}| None              | Serve avatar image      |
| DELETE | /auth/me/avatar       | Bearer Token      | Remove avatar           |
| GET    | /auth/me/profile      | Bearer Token      | Get user profile        |
| PUT    | /auth/me/profile      | Bearer Token      | Update user profile     |
| GET    | /auth/me/preferences  | Bearer Token      | Get preferences         |
| PUT    | /auth/me/preferences  | Bearer Token      | Create/update prefs     |
| POST   | /auth/forgot-password | None              | Request password reset  |
| POST   | /auth/reset-password  | Token in body     | Complete password reset |

### Laptops (`/laptops`)

| Method | Endpoint                   | Auth       | Purpose              |
| ------ | -------------------------- | ---------- | -------------------- |
| POST   | /laptops/                  | None       | Create laptop        |
| GET    | /laptops/                  | None       | List all laptops     |
| GET    | /laptops/raw-scrap-laptops | Admin only | List raw scraped data|
| GET    | /laptops/{laptop_id}       | None       | Get specific laptop  |
| PUT    | /laptops/{laptop_id}       | None       | Update laptop        |
| DELETE | /laptops/{laptop_id}       | None       | Delete laptop        |

### Brands (`/brands`)

| Method | Endpoint        | Auth       | Purpose                 |
| ------ | --------------- | ---------- | ----------------------- |
| POST   | /brands         | Admin only | Create new brand        |
| GET    | /brands         | None       | List brands (paginated) |
| GET    | /brands/{id}    | None       | Get specific brand      |
| PUT    | /brands/{id}    | Admin only | Update brand            |
| DELETE | /brands/{id}    | Admin only | Delete brand            |

### Customizations (`/customizations`)

| Method | Endpoint                           | Auth       | Purpose                      |
| ------ | ---------------------------------- | ---------- | ---------------------------- |
| POST   | /customizations/                   | Admin only | Bulk create customizations   |
| POST   | /customizations/bulk-by-pattern    | Admin only | Bulk create by model pattern |
| GET    | /customizations/laptop/{laptop_id} | Admin only | Get by laptop                |
| PATCH  | /customizations/{id}               | Admin only | Update customization         |
| DELETE | /customizations/{id}               | Admin only | Delete customization         |

### Scraper (`/scraper`)

| Method | Endpoint                            | Auth       | Purpose                                    |
| ------ | ----------------------------------- | ---------- | ------------------------------------------ |
| POST   | /scraper/feed-crawler               | Admin only | Crawl site for spec page links             |
| POST   | /scraper/scrape-url                 | Admin only | Scrape a single URL                        |
| POST   | /scraper/bulk-scrape                | Admin only | Bulk scrape all pending URLs for a brand   |
| GET    | /scraper/raw-laptop/{raw_laptop_id} | Admin only | Get full details of a raw scraped record   |

### Processor (`/processor`)

| Method | Endpoint                         | Auth       | Purpose                     |
| ------ | -------------------------------- | ---------- | --------------------------- |
| POST   | /processor/process/{raw_id}      | Admin only | Process single raw laptop   |
| POST   | /processor/process-pending       | Admin only | Batch process all pending   |
| POST   | /processor/categorize-untagged   | Admin only | Backfill category tags for untagged laptops |

### Benchmarks (`/benchmarks`)

| Method | Endpoint                | Auth       | Purpose                         |
| ------ | ----------------------- | ---------- | ------------------------------- |
| POST   | /benchmarks/cpu         | Admin only | Create CPU benchmark            |
| GET    | /benchmarks/cpu         | None       | List CPU benchmarks             |
| GET    | /benchmarks/cpu/{id}    | None       | Get specific CPU benchmark      |
| PATCH  | /benchmarks/cpu/{id}    | Admin only | Update CPU benchmark            |
| DELETE | /benchmarks/cpu/{id}    | Admin only | Delete CPU benchmark            |
| POST   | /benchmarks/scrape/cpu  | Admin only | Trigger PassMark CPU scraper    |
| POST   | /benchmarks/gpu         | Admin only | Create GPU benchmark            |
| GET    | /benchmarks/gpu         | None       | List GPU benchmarks             |
| GET    | /benchmarks/gpu/{id}    | None       | Get specific GPU benchmark      |
| PATCH  | /benchmarks/gpu/{id}    | Admin only | Update GPU benchmark            |
| DELETE | /benchmarks/gpu/{id}    | Admin only | Delete GPU benchmark            |
| POST   | /benchmarks/scrape/gpu  | Admin only | Trigger PassMark GPU scraper    |

### Embeddings (`/embeddings`)

| Method | Endpoint                     | Auth       | Purpose                              |
| ------ | ---------------------------- | ---------- | ------------------------------------ |
| POST   | /embeddings/generate-all     | Admin only | Generate embeddings for all laptops  |
| POST   | /embeddings/generate/{id}    | Admin only | Generate embedding for single laptop |
| GET    | /embeddings/status           | Admin only | Count embedded vs total laptops      |

### Pick Score (`/laptops`)

| Method | Endpoint                          | Auth | Purpose                              |
| ------ | --------------------------------- | ---- | ------------------------------------ |
| POST   | /laptops/calculate-score          | None | Single laptop PickScore              |
| POST   | /laptops/calculate-score/batch    | None | Batch PickScore                      |
| GET    | /laptops/{id}/pick-scores         | None | Stored use-case PickScores for one laptop |
| GET    | /laptops/pick-scores/ranking      | None | Use-case ranking by stored PickScore |
| POST   | /laptops/pick-scores/generate-all | Admin only | Recompute all stored use-case PickScores |
| POST   | /laptops/hybrid-search            | None | pgvector semantic search             |
| GET    | /laptops/{id}/price-history       | None | Laptop price snapshot series         |

### Recommendations (`/recommendations`)

| Method | Endpoint                    | Auth         | Purpose                                |
| ------ | --------------------------- | ------------ | -------------------------------------- |
| POST   | /recommendations/laptops    | Bearer Token | Full recommendation pipeline (top 3)   |

### Conversations (`/conversations`) — ✅ Complete

| Method | Endpoint                  | Auth         | Purpose                                              |
| ------ | ------------------------- | ------------ | ---------------------------------------------------- |
| POST   | /conversations/           | Bearer Token | Create new conversation thread                       |
| GET    | /conversations/           | Bearer Token | List user's conversations (title + timestamps)       |
| GET    | /conversations/{id}       | Bearer Token | Get conversation with full message history           |
| DELETE | /conversations/{id}       | Bearer Token | Delete conversation and all messages                 |

Chat itself is `POST /agent/chat` below — CRS's `/{id}/chat` route was removed when its pipeline was absorbed into the agent's `search_laptops` tool.

### Agent (`/agent`)

| Method | Endpoint    | Auth         | Purpose                                                                     |
| ------ | ----------- | ------------ | ---------------------------------------------------------------------------- |
| POST   | /agent/chat | Bearer Token | ReAct agent — search (with PickScore) + pricing + review evidence + market prices; returns text reply + structured `laptops` shortlist |

### Reviews (`/reviews`)

| Method | Endpoint                       | Auth       | Purpose                                      |
| ------ | ------------------------------ | ---------- | -------------------------------------------- |
| POST   | /reviews/channels              | Admin only | Resolve channel URL + create channel         |
| GET    | /reviews/channels              | Admin only | List all channels                            |
| PATCH  | /reviews/channels/{id}         | Admin only | Update channel metadata                      |
| POST   | /reviews/ingest/{laptop_id}    | Admin only | Full discovery + transcript + match pipeline |
| POST   | /reviews/ingest-bulk           | Admin only | Bulk discovery, one search per laptop family |
| GET    | /reviews/raw                   | Admin only | List raw reviews (filterable by status)      |
| PATCH  | /reviews/raw/{id}/match        | Admin only | Manually pair review to laptop               |
| POST   | /reviews/rematch               | Admin only | Re-run auto-match on all pending reviews     |
| POST   | /reviews/process/{review_id}   | Admin only | Chunk + embed a matched review               |
| POST   | /reviews/aggregate/{laptop_id} | Admin only | Recompute laptop review summary              |

### Taxonomy (`/product-types`, `/categories`)

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST   | /product-types      | Admin only | Create product type |
| GET    | /product-types      | None       | List product types |
| GET    | /product-types/{id} | None       | Get product type |
| PUT    | /product-types/{id} | Admin only | Update product type |
| DELETE | /product-types/{id} | Admin only | Delete (409 if questionnaire questions reference it) |
| POST   | /categories         | Admin only | Create category (tag) |
| GET    | /categories         | None       | List categories |
| GET    | /categories/{id}    | None       | Get category |
| PUT    | /categories/{id}    | Admin only | Update category |
| DELETE | /categories/{id}    | Admin only | Delete (409 if tagged/referenced) |

### Questionnaire (`/questionnaire`)

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| POST | /questionnaire | Admin only | Create question (404 unknown product type, 409 active step conflict) |
| GET | /questionnaire | None | Questions for a product type ordered by step (active only unless `include_inactive=true`) |
| GET | /questionnaire/{id} | None | Get specific question |
| PUT | /questionnaire/{id} | Admin only | Update question (same 404/409 checks) |
| DELETE | /questionnaire/{id} | Admin only | Hard delete (prefer `is_active=false` to retire) |

### Health Check

| Method | Endpoint | Auth | Purpose         |
| ------ | -------- | ---- | --------------- |
| GET    | /        | None | Health check    |

---

## 🎯 Key Implementation Details

### Data Pipeline Architecture

```
1. Feed Crawler → Discovers spec URLs     → laptop_scrape_urls
2. Scrape URL   → Scrapes raw HTML/JSON   → raw_scrap_laptops (status: pending)
   Bulk Scrape  → Batch scrapes all       → raw_scrap_laptops (status: pending)
3. AI Processor → LLM extraction          → laptops (normalized, multi-variant)
4. Benchmarks   → PassMark scraping       → cpu_benchmarks / gpu_benchmarks
```

### Email Verification Workflow

1. User registers → Email verification JWT created (scoped)
2. Email sent with verification link (background task)
3. User clicks link → Calls verify-email endpoint
4. Token validated (scope check) → User marked as verified
5. User can now login

### Password Reset Workflow

1. User submits email → Reset token generated (15 min expiry)
2. Email sent with reset link (background task, points to frontend: `localhost:3000`)
3. User clicks link → Enters new password
4. Token validated (scope check) → Password updated (bcrypt hashed)
5. Safe messaging: response doesn't reveal if user exists

### AI Processing Workflow

1. Raw data fetched from staging table
2. Brand resolved for context
3. System prompt with detailed extraction rules sent to Gemini (`gemini-3.5-flash`)
4. LLM returns structured `ExtractedLaptopFamily` with multiple variants, following combinatorial SKU and pricing matrix isolation rules
5. Each variant mapped to `Laptop` model and saved
6. Duplicate SKUs caught via `IntegrityError` → rollback + skip
7. Raw record status updated to 'completed' or 'failed'

### Benchmark Scraping Workflow

1. Admin triggers scrape via `/benchmarks/scrape/cpu` or `/benchmarks/scrape/gpu`
2. Playwright launches headless Chromium, navigates to PassMark
3. JavaScript evaluated in-page to extract table data (name + score)
4. Data upserted to PostgreSQL using `ON CONFLICT DO UPDATE`
5. Runs as background task to prevent API worker timeout

### Bulk Scraping Workflow

1. Admin triggers via `/scraper/bulk-scrape` with brand_id
2. System queries all `ScrapeTarget` rows where `is_active=True` and `last_scraped_at IS NULL`
3. Each pending URL is checked for existing scraped data (duplicate detection)
4. URLs dispatched to brand-specific scraper (Apple → `scrape_official_website`, Asus → `scrape_asus_laptop_specs`)
5. `last_scraped_at` stamped on ScrapeTarget regardless of outcome
6. Successful variants saved to `raw_scrap_laptops` with `processing_status = "pending"`
7. Returns structured report with per-URL status (succeeded/skipped/failed)
8. HTTP 200 on full success, HTTP 207 Multi-Status on partial failures
9. Timestamped failure log written to `logs/scraper/` if any URLs failed

### Preference Management Workflow

1. User authenticated via JWT token
2. GET preferences → Returns stored preference record (or empty defaults)
3. PUT preferences → Creates new record if not exists, or updates existing
4. `updated_at` timestamp maintained automatically

---

## ✨ Features Implemented

- ✅ User registration with strong password requirements
- ✅ Email verification system (JWT-scoped, SMTP/SSL)
- ✅ JWT-based authentication (configurable 7-day expiry)
- ✅ Login with username **or** email (single OR query on two unique indexed columns)
- ✅ Google Sign-In (`POST /auth/google`) — ID-token verification, find-or-create + email linking, auto-verified, nullable password
- ✅ Password reset via email (15-min token expiry)
- ✅ User profile management (birthday, gender, occupation)
- ✅ User avatar gateway — bytea storage in `user_avatars`, magic-byte validation (JPEG/PNG/WebP), 2 MB cap, public serve endpoint with cache headers
- ✅ Laptop preference system with multiple criteria
- ✅ Tech-savviness level tracking (validated enum)
- ✅ Partial update support (profile, preferences, brands, customizations, benchmarks)
- ✅ Background email tasks (FastAPI BackgroundTasks)
- ✅ Full database migrations with rollback support (29 migrations, see 📦 Database Migrations table)
- ✅ Input validation with custom field validators
- ✅ Error handling with appropriate HTTP status codes
- ✅ 9-part comprehensive laptop specification model (50+ fields)
- ✅ UUID-based brand system with foreign key references
- ✅ Brand CRUD with admin-only write protection
- ✅ Laptop customization/upgrade tracking system
- ✅ Bulk customization creation (assign to multiple laptops)
- ✅ Bulk customization creation by model pattern matching
- ✅ Web scraping pipeline (Playwright + Chromium headless)
- ✅ Apple specs page crawler and extractor (async, DOM-based)
- ✅ Asus/ROG specs page crawler and extractor (async, `__NUXT__` JSON extraction)
- ✅ Asus scraper: dual-path extraction (ROG → `Spec.spec`, Standard ASUS → `PDPage.TechSpec`)
- ✅ Asus scraper: variant-aware with per-SKU price resolution and `?v=N` suffix
- ✅ Bulk scraping orchestration (process all pending URLs for a brand in one API call)
- ✅ Bulk scrape failure logging (timestamped logs to `logs/scraper/`)
- ✅ Bulk scrape HTTP 207 Multi-Status on partial failures
- ✅ Raw scraped laptop detail endpoint (view full raw data by ID)
- ✅ Raw data staging table with processing status tracking
- ✅ AI-powered data extraction (Google Gemini `gemini-3.5-flash` via LangChain)
- ✅ Structured LLM output with Pydantic validation
- ✅ Combinatorial variant extraction with safety rules and price matrix isolation
- ✅ Batch processing for pending raw records
- ✅ Duplicate SKU protection (IntegrityError handling)
- ✅ Duplicate scrape detection (source_url LIKE check before scraping)
- ✅ Vector embedding table for future RAG/similarity search (pgvector 768-dim)
- ✅ Role-based access control (user/admin roles)
- ✅ Router architecture separation (users, laptops, brands, customizations, scraper, processor, benchmarks)
- ✅ Docker Compose for PostgreSQL + pgvector
- ✅ Windows event loop compatibility (running Playwright inside dedicated thread/loop)
- ✅ CPU benchmark scoring system (PassMark CPUMark)
- ✅ GPU benchmark scoring system (PassMark G3D Mark)
- ✅ Automated PassMark scraping with PostgreSQL upsert
- ✅ Background task execution for long-running scrapers
- ✅ TYPE_CHECKING circular import resolution pattern
- ✅ PickScore v2 deterministic engine (8 factors, 3-layer weighting, personalized + general modes)
- ✅ Apple Silicon GPU proxy scoring (ARM SoC — always reuses CPU score, no false PassMark GPU matches)
- ✅ Benchmark range calibration from catalog laptops only (prevents desktop CPU score inflation)
- ✅ RapidFuzz fuzzy benchmark matching (confidence threshold 0.6, 5-min module-level cache)
- ✅ Product-agnostic PickScore adapter pattern (extensible to phones, tablets)
- ✅ Laptop embeddings (gemini-embedding-2, 768-dim pgvector via `output_dimensionality`, natural-language document format)
- ✅ Hybrid vector search (pgvector cosine distance + budget/brand hard filters)
- ✅ Price history tracking (snapshot on create + on every PUT price change)
- ✅ LLM recommendation layer (hybrid search → PickScore → Gemini LLM explanations)
- ✅ Per-laptop LLM explanations + overall summary (structured Gemini output, no parsing fragility)
- ✅ tech_savviness-adaptive LLM language (3 levels: Very / Somewhat / Not tech-savvy)
- ✅ Recommendation auth guard — requires Bearer token + preference profile (400 if missing)
- ✅ Conversational history & memory (conversations + messages + conversation_laptops tables)
- ✅ CRS Module 1 — Online retrieval: pgvector top-50 recall, 5-min query cache, relational fallback
- ✅ CRS Module 2 — Reranking: budget + weight penalty multipliers, purpose + brand bonuses, decoupled UserConstraints
- ✅ CRS Module 3 — Constraint relaxation: stepwise weight → budget relaxation, brand never auto-relaxed
- ✅ CRS Module 4 — Relevance gating: threshold 0.53 (calibrated for gemini-embedding-2 similarity scale), bottleneck detection, targeted clarification messages
- ✅ CRS Module 5 — NDCG@10 offline evaluation + live pipeline logging per user request
- ✅ pipeline_eval_logs table — queryable quality trace (gate_status, top_score, relaxed_field, bottleneck)
- ✅ logs/eval/pipeline_trace.jsonl — structured JSONL file log, survives DB failures, greppable in CI
- ✅ Intent detection via Gemini (related vs new_search) on every follow-up message
- ✅ conversation_laptops pool replacement on new_search, reused on related follow-ups
- ✅ Auto-title from first message (truncated to 60 chars)
- ✅ ReAct agent (`app/agent/`) — `search_laptops` + `calculate_custom_apple_price` + `get_review_evidence` + `search_malaysian_market_price` tools, all query DB directly
- ✅ Agent endpoint `POST /agent/chat` (auth required, 503 on agent error with diagnostic message)
- ✅ Two-layer Malaysian market price tool — catalog price + history from own DB, live listings via SerpApi Google Shopping (accessory blocklist, RM 800 floor, per-store cap, 6h cache)
- ✅ General-mode PickScore attached to agent search results (`pick_score` 0–100 + top-3 factor summary, batch-computed on gated top-k; non-fatal on failure)
- ✅ Structured `laptops` shortlist in `/agent/chat` response (`AgentLaptopCard`) for frontend score badges — fresh results or persisted pool on follow-up turns
- ✅ `pick_score` persisted to `conversation_laptops` alongside `similarity_score`
- ✅ System prompt enforcement blocks — scope (laptop-only), factual grounding (no prices from memory), PickScore citation ("PickScore N/100", never invent scores)
- ✅ Gemini list-of-blocks reply flattening (`_content_to_text`) — fixed `psycopg2 can't adapt type 'dict'` crash on message persistence
- ✅ Agent eval harness (`eval/`) — 30 bilingual queries × 5 categories, deterministic rule checks + grounded LLM judge, JSONL runs + `compare` regression diff (latest run: 28/30, 0 unscored)
- ✅ YouTube channel management — URL-based resolution (handles `@handle`, `/channel/UCxx`, bare ID), `trust_tier`, `active` flag
- ✅ YouTube video discovery — `search.list` per channel, top-5 per query, 100 quota units/channel
- ✅ Transcript fetching — `youtube-transcript-api` v1.x instance API (`YouTubeTranscriptApi().fetch()`), no quota cost
- ✅ Laptop-to-video fuzzy matching — compact match key (strips `-inch`/RAM/storage, extracts chip), `token_set_ratio` threshold 73
- ✅ Review ingest retry logic — `rejected` rows re-attempted on next run; `matched`/`pending` skipped
- ✅ `POST /reviews/rematch` — bulk re-run auto-matching on all pending rows after threshold/key changes
- ✅ `POST /reviews/ingest-bulk` — quota-aware bulk discovery: catalog collapsed to laptop families (74 from 245 variants), one YouTube search per family, `skip_covered` walks the catalog across daily quota windows
- ✅ Review chunking — 45-second windows, Gemini summary + sentiment tag (`strength`/`weakness`/`neutral`)
- ✅ Review embedding — central `embed_text()` (gemini-embedding-2, 768-dim), stored in `laptop_review_chunks`
- ✅ Review aggregation — top-5 distinct strengths + weaknesses rolled up to `laptop_review_summary`
- ✅ `get_review_evidence` agent tool — pgvector cosine search on chunks, returns top-3 with YouTube timestamp links
- ✅ Apple scraper image hygiene — og:image/`/meta/`/`_og.` social-preview cards excluded at scrape time
- ✅ Per-variant image filtering in AI processor — each Laptop variant keeps only its own screen size's images (`NN-inch` URL token vs `display_size_inch`) plus size-agnostic shots
- ✅ Agent LLM migrated to OpenRouter — `nvidia/nemotron-3-ultra-550b-a55b:free` via `ChatOpenAI` + `build_agent_llm()` factory (shared with eval harness); `OPENROUTER_API_KEY` required
- ✅ Embeddings migrated to OpenRouter — `nvidia/llama-nemotron-embed-vl-1b-v2:free`, `dimensions=768` Matryoshka truncation (no schema migration), all 245 laptops re-embedded, `embed_text()` retry hardening
- ✅ Gate thresholds recalibrated for new embedding space — `RELEVANCE_THRESHOLD` 0.40 → 0.20, `_MIN_VIABLE_SCORE` 0.25 → 0.13 (verified end-to-end: relevant queries pass, off-catalog queries gated)
- ✅ AI category tagging in processor — LLM picks 1–3 use-case tags per variant against DB categories, auto-creates unknown tags, additive linking via `laptop_categories`
- ✅ Category backfill endpoint — `POST /processor/categorize-untagged` tags laptops with zero category links from stored specs
- ✅ Precomputed use-case PickScores — `laptop_pick_scores` table (laptop × 5 use-case weight profiles, 1,225 rows), public per-laptop + ranking endpoints, admin regenerate
- ✅ Gaming ranking proxy demotion — Apple CPU-proxied GPU scores sort below real-benchmark laptops in the gaming use case
- ✅ Agent LLM reverted to Gemma (2026-07-17) — `gemma-4-31b-it` via `ChatGoogleGenerativeAI`, `build_agent_llm()` factory kept; `OPENROUTER_API_KEY` no longer required
- ✅ Embeddings switched to `gemini-embedding-2` (2026-07-17) — `output_dimensionality=768`, all 245 laptops re-embedded, gate thresholds recalibrated by live-catalog spot-check (0.53 / 0.33)
- ✅ SSE streaming chat endpoint `POST /agent/chat/stream` (2026-07-18) — token-by-token reply + tool-activity events via `stream_agent()`/`astream_events`; thinking-block filtering, internal tool-call-turn text discarded via `turn_reset`, persistence after stream completion shared with the non-streaming endpoint

---

## 📝 Notes

- All passwords are hashed using bcrypt before storage (nullable for Google-created accounts)
- Email verification is required before login (Google accounts are auto-verified by the provider)
- JWT tokens expire after configured duration (default: 7 days)
- Password reset tokens expire after 15 minutes
- Database uses UUID for all primary keys across all tables
- Timestamps use UTC timezone
- Database SQL echo is disabled (`echo=False`) for production readiness
- Support for partial updates on profile, preferences, brands, customizations, and benchmarks
- Tech-savviness is optional but validated when provided
- **Brand system** uses UUID foreign keys instead of string references
- **Router separation** prevents circular imports and improves maintainability
- **Type system** strictly separates Python `uuid.UUID` from SQLAlchemy UUID types
- **Import architecture** uses `TYPE_CHECKING` guards + deferred imports to resolve Laptop ↔ LaptopCustomization circular dependency
- **Scraping** supports Apple and Asus brands; architecture is extensible for other brands
- **Asus scraper** uses `window.__NUXT__` state extraction (no CSS selectors) for reliable, variant-aware data extraction — separate paths for ROG (`Spec.spec`) and standard ASUS (`PDPage.TechSpec`)
- **Bulk scraper** orchestrates brand-wide scraping with structured reporting, failure logging, and HTTP 207 Multi-Status support
- **AI Processor** uses `gemini-3.5-flash` with temperature=0 for deterministic, high-quality extraction
- **AI Processor** imports `LaptopBrand` from `brand_model.py` (not `laptop_models.py`) to avoid circular dependency
- **pgvector** extension is auto-initialized in `database.py` for future embedding features
- **Customization system** supports dependency notes for documenting hardware constraints
- **LaptopBrand.icons_url** was initially NOT NULL but migrated to nullable for flexibility
- **Raw scrap data** includes `image_urls` (array) and `raw_prices` (array) for multi-price support
- **Benchmark scrapers** use PostgreSQL-native `INSERT ... ON CONFLICT DO UPDATE` for idempotent data sync
- **Windows compatibility**: Playwright is executed on a dedicated background thread with its own `ProactorEventLoop` to solve Uvicorn event loop errors on Windows
- **Progress.md** is listed in `.gitignore` and will not be uploaded to GitHub
