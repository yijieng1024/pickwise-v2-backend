# PickWise Admin Portal — Endpoint & Flow Reference

**Audience:** UI/UX designers building the admin portal.
**Purpose:** what every admin capability does, the order things must happen in, and where the experience will hurt if the UI ignores it.

You do not need to read any code. Every endpoint below is real and verified against the running backend (62 admin endpoints as of 2026-08-05).

**Conventions used here**

- All paths are prefixed with `/api/v2`. `POST /brands` means `POST /api/v2/brands`.
- 🔴 **Destructive** — needs a confirmation step.
- 🔄 **Background job** — returns `202` with a `job_id`; poll for progress. See [§9](#9-long-running-operations--now-polled-background-jobs).
- ⏳ **Slow (blocking)** — the request does not return for minutes. Only `/reviews/process-bulk` still behaves this way.
- 🚀 **Fires and forgets** — returns instantly, work continues invisibly with no way to poll.
- 💸 **Quota-limited** — costs a limited daily external allowance. The UI must not encourage repeat clicking.

---

## 1. Who gets in, and what happens when they don't

Admin is a **role on a normal user account** (`role: "admin"`), not a separate login. The same email/password (or Google) sign-in is used, and the resulting token is sent on every admin request.

| Situation | Backend response | What the UI should do |
|---|---|---|
| Not signed in / token expired | `401` | Bounce to login, preserve the intended destination |
| Signed in but `role != "admin"` | `403` | Do **not** show the admin nav at all. If reached directly, show a plain "no access" page — no error jargon |
| Account `status` is not `active` | `401` at login and on every request | Explain the account is inactive/suspended, offer support contact |

> **Design note:** a token lasts 7 days. Someone can be mid-task when it expires. Any failed action should be recoverable — don't lose an in-progress form or a selected batch on a 401.

---

## 2. The system map — read this before designing any screen

Everything in this portal exists to move laptop data along one pipeline. Each stage **depends on the one before it**. This is the mental model the UI has to teach.

```
   ┌─────────────────────────────────────────────────────────────────┐
   │  STAGE 1 — FIND PRODUCT PAGES                                   │
   │  Feed crawler  ─────────────►  laptop_scrape_urls (the queue)   │
   │                                        ▲                        │
   │            Upload saved HTML ──────────┘  (Acer only)           │
   └────────────────────────┬────────────────────────────────────────┘
                            ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  STAGE 2 — COLLECT RAW SPECS                                    │
   │  Scrape (live) or parse (uploaded)  ──►  raw_scrap_laptops      │
   │  Messy vendor text. Not shown to customers.                     │
   └────────────────────────┬────────────────────────────────────────┘
                            ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  STAGE 3 — AI CLEAN-UP                                          │
   │  AI processor  ──►  laptops  (the real catalog + auto-tagging)  │
   └────────────────────────┬────────────────────────────────────────┘
                            ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  STAGE 4 — MAKE IT SEARCHABLE                                   │
   │  Embeddings  ──►  vector search        (chat can find it)       │
   │  PickScores  ──►  ranking scores       (cards show a score)     │
   └─────────────────────────────────────────────────────────────────┘

   Feeding in from the side:
     Benchmarks (CPU/GPU) ──► needed for PickScore to be meaningful
     YouTube reviews      ──► gives the chatbot real reviewer quotes
```

**The UX consequence:** a laptop that has been scraped but not processed is invisible to customers. One that is processed but not embedded is invisible to the chatbot. One that is embedded but has no PickScore shows a blank score badge.

> **Strong recommendation:** the admin dashboard should be a **pipeline health view**, not a list of buttons. Show the count stuck at each stage, and make the number itself the call to action — *"38 scraped laptops waiting to be processed →"*. Admins should never have to remember what runs after what.

---

## 3. Stage 1 — Getting product pages into the queue

Two routes into the queue, because one manufacturer cannot be crawled at all.

### 3a. Normal brands (Apple, Asus)

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/scraper/feed-crawler` | Visits the brand's own website and collects every laptop product-page link it finds, adding new ones to the queue |
| `GET` | `/scraper/targets` | The queue itself — filter by `brand_id`, `scrape_status`, `is_active`; paginated (`limit` 1–1000, default 100) |

The crawler takes **only a `brand_id`**. The starting website address is stored on the brand record and cannot be typed in by the admin — so there is no URL field to design, and no way to point one brand's crawler at another brand's site.

Response tells you `total_found` and `added_to_queue` — already-known links are silently skipped. Surface both, because "found 49, added 0" is a *success* (nothing new), not a failure, and the UI must not make it look alarming.

### 3b. Acer — the manual upload path 🔴 *needs the most design care*

**Acer's store blocks all automated access.** Not a bug, not fixable — their protection system rejects our servers outright. The workaround: a human opens each product page in a normal browser, saves it (`Ctrl+S` → "Webpage, HTML Only"), and uploads the file.

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/scraper/upload-html` | Upload one or many saved `.html` files (`multipart/form-data`) |
| `POST` | `/scraper/upload-html/json` | Same, for tools that already hold the HTML as text |

Optional fields: `product_type` (defaults to `"laptop"`) and `brand_id` (recommended — restricts matching to one brand so a mis-filed page is caught).

**Filenames do not matter.** Each saved page contains a hidden tag identifying which product it is; the backend reads that and matches it to the right queue row automatically. *Tell the admin this in the UI* — otherwise they will invent a careful naming convention for nothing.

**Every file is judged independently.** Uploading 49 files where 3 are bad still stores the other 46. The response is a summary:

```
received: 49    matched: 46    unmatched: 2    invalid: 1
inserted: 40    updated: 6
+ a per-file row with its own status and error message
```

| Per-file status | Meaning | Suggested UI |
|---|---|---|
| `matched` | Stored successfully | ✅ green; show `created: true` as "new", `false` as "updated" |
| `unmatched` | Valid page, but no matching row in the queue (wrong brand, or not crawled yet) | ⚠️ amber — recoverable, not the admin's mistake |
| `invalid` | Not a product page — usually the browser saved a "checking your browser" screen instead | ❌ red — tell them to **re-save the page and try again** |

**Design for this screen:**
- Drag-and-drop, **multi-file**, with a per-file result list. A single toast is useless for 49 files.
- Re-uploading the same page is *normal and encouraged* — it refreshes a stale price. Never warn about "duplicates".
- Show which queue items still have no HTML: `GET /scraper/targets?brand_id=…&scrape_status=failed`. This is the admin's to-do list — arguably the primary view of this screen.
- Consider showing the product name next to each expected upload so the admin knows which page to open next.

---

## 4. Stage 2 — Collecting the raw specs

| Method | Endpoint | What it does | Notes |
|---|---|---|---|
| `POST` | `/scraper/bulk-scrape` | Processes the whole queue for one brand | 🔄 job |
| `POST` | `/scraper/scrape-targets` | Processes only the rows the admin ticked | 🔄 job |
| `POST` | `/scraper/scrape-url` | One single web address | inline |
| `GET` | `/scraper/raw-laptop/{id}` | Inspect one raw record in full | |
| `GET` | `/laptops/raw-scrap-laptops` | Browse everything collected (`offset`, `limit` default 50) | |

🔄 = returns `202` with a `job_id`; see [§9](#9-long-running-operations--now-polled-background-jobs).

**Bulk vs. selected:** bulk picks up anything not yet done or previously failed. Selected always retries whatever was ticked, even if it succeeded before. Both are useful — expose both, but make bulk the primary action and selection the power-user path.

### Partial success is the normal case ⚠️

A run where some items fail is **not** a failed run. The job finishes as `completed` with a non-zero `failed_count`; the failures are in `errors[]` (live, as they happen) and the finished job's `result` carries the per-URL detail plus a `log_file` path.

Design a **result summary panel**: `succeeded / failed / skipped`, with the failures expandable and each carrying its own readable reason. A red error toast for a run where 45 of 49 worked is actively misleading.

**Status badges** — the queue's `scrape_status` needs six distinct visual states:

| Status | Meaning | Feel |
|---|---|---|
| `pending` | Found, not yet collected | neutral |
| `html_uploaded` | *(Acer)* file received, waiting to be parsed | in-progress |
| `parsed` | *(Acer)* done, from an uploaded file | success |
| `completed` | Done, scraped live | success |
| `failed` | Something went wrong — **retried automatically next run** | warning, not alarm |
| `skipped` | Already had data, left alone | muted |

`parsed` and `completed` are both success; they differ only in *how* the data arrived. Consider one green badge with a small "uploaded" vs "scraped" qualifier rather than two colours.

---

## 5. Stage 3 — AI clean-up

Turns messy vendor text into real catalog entries, and auto-assigns use-case tags (Gaming, Business…).

| Method | Endpoint | What it does | Notes |
|---|---|---|---|
| `POST` | `/processor/process-pending` | Process a batch of collected records. `limit` 1–1500, **default 100** | 🔄💸 |
| `POST` | `/processor/process/{id}` | Process one record | 💸 inline |
| `POST` | `/processor/categorize-untagged` | Add missing use-case tags to existing laptops. `limit` 1–1500, default 100 | 🔄💸 |

**The timing reality:** the AI provider is on a free tier, so the backend deliberately waits **5 seconds between records**. A batch of 100 takes roughly 8 minutes — but the request no longer waits for it (🔄, see [§9](#9-long-running-operations--now-polled-background-jobs)).

**Design implications:**
- Let the admin **choose the batch size**, and show the estimate updating live as they change it — or just use `estimated_seconds` from the 202 response.
- The 202's `total_count` is the **actual** number pending, which is usually smaller than the requested limit. Show that back rather than echoing their input.
- The daily allowance is 1,500 records. If they exceed it, everything fails until tomorrow — worth a visible daily counter.
- Tagging is **additive**: it never removes a tag an admin set by hand. Say so in the UI; it makes the button safe to press.
- Single-record processing stays inline — one LLM call is fast enough to answer directly.

---

## 6. Stage 4 — Making laptops findable and rankable

| Method | Endpoint | What it does | Notes |
|---|---|---|---|
| `POST` | `/embeddings/laptops/generate-all` | Makes every laptop searchable by the chatbot | 🚀 |
| `POST` | `/embeddings/laptops/{laptop_id}` | Same, one laptop | 🚀 |
| `GET` | `/embeddings/laptops/status` | How many laptops are searchable vs. total | *(public — no admin token needed)* |
| `POST` | `/laptops/pick-scores/generate-all` | Recalculates the 0–100 scores shown on laptop cards | ⏳ |

**These two return immediately** (🚀) but the work continues invisibly. There is **no progress feed** — the only way to know is to poll `/embeddings/laptops/status` and watch the "embedded" count climb.

> **Design this explicitly:** after triggering, switch to a polling progress display driven by that status endpoint. Without it the admin has no idea whether anything happened, and will press the button repeatedly.

PickScores are pure arithmetic — no AI, no waiting on an external service — so this one is comparatively quick and completely safe to re-run.

**When these need re-running** (the UI should prompt, not rely on memory):
- After importing new laptops → both
- After refreshing benchmarks → PickScores
- **Whenever the AI embedding model is changed → embeddings must be fully regenerated,** or search silently returns nonsense. This is a footgun; if the portal ever exposes a model setting, guard it hard.

---

## 7. Managing the catalog itself

### Laptops

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/laptops/` | Create a laptop by hand |
| `PUT` | `/laptops/{id}` | Edit a laptop |
| `DELETE` | `/laptops/{id}` | 🔴 Delete a laptop |

A laptop has **~200 fields across 9 groups** (identity, processor, graphics, memory/storage, display, build/battery/ports, input/audio, security/warranty, plus raw data and images).

> A single flat form is unusable at this size. Use the 9 groups as **collapsible sections or a stepper**, and let most fields be empty — real listings rarely specify everything. Price and model code matter most; put them first.

### Brands

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/brands` | Add a brand |
| `PUT` | `/brands/{id}` | Edit a brand |
| `DELETE` | `/brands/{id}` | 🔴 Delete a brand |

A brand holds the website address the crawler starts from — **editing it changes what the crawler will collect**. Flag that field visually; it has consequences far from this screen.

Deleting a brand still referenced by laptops returns **`409`**. Don't present this as an error — present it as *"12 laptops still use this brand"* with a link to them.

### Customizations (upgrade options, e.g. "+16GB RAM — RM 800")

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/customizations/` | Add one option to **many laptops at once** |
| `POST` | `/customizations/bulk-by-pattern` | Add to every laptop whose model code matches a pattern |
| `GET` | `/customizations/laptop/{laptop_id}` | Options for one laptop |
| `GET` | `/customizations/laptops-summary` | Overview of which laptops have options |
| `PATCH` | `/customizations/{id}` | Edit one option |
| `DELETE` | `/customizations/{id}` | 🔴 Remove one option |

The pattern tool is powerful and blind — typing `m5-max` hits every matching laptop at once. **Show a preview of what will be affected before committing.** There is no undo.

---

## 8. Supporting data

### Taxonomy — product types, tags, and the customer questionnaire

| Method | Endpoint | What it does |
|---|---|---|
| `POST` `PUT` `DELETE` | `/product-types[/{id}]` | Product lines (currently just "laptop"; phones/monitors later) |
| `POST` `PUT` `DELETE` | `/categories[/{id}]` | Marketing tags shown to customers (Gaming, Business, Creator…) |
| `POST` `PUT` `DELETE` | `/questionnaire[/{id}]` | The 6-step questions new customers answer |

All three return **`409`** when deleting something still in use, or when creating a duplicate. Same guidance as brands: explain *what* is blocking, don't just say "conflict".

The questionnaire is **customer-facing** — editing it changes what real users see immediately, and answers drive their personalized scores. This screen deserves a preview and a heavier confirmation than the rest. Two active questions cannot share the same step position (also a `409`); a drag-to-reorder interface should handle ordering rather than exposing raw step numbers.

### Benchmarks (CPU/GPU performance data)

| Method | Endpoint | What it does | Notes |
|---|---|---|---|
| `POST` | `/benchmarks/scrape/cpu` | Refresh all CPU scores from PassMark | 🚀 |
| `POST` | `/benchmarks/scrape/gpu` | Refresh all GPU scores | 🚀 |
| `POST` | `/benchmarks/cpu` · `/benchmarks/gpu` | Add one entry manually | |
| `PATCH` | `/benchmarks/cpu/{id}` · `/benchmarks/gpu/{id}` | Edit one entry | |
| `DELETE` | `/benchmarks/cpu/{id}` · `/benchmarks/gpu/{id}` | 🔴 Delete one entry | |

The two scrapers return instantly with no progress and no completion signal (🚀) — the same invisible-work problem as embeddings, but here there is **no status endpoint to poll**. Best available honesty: *"Refresh started. This runs in the background and may take a few minutes."* Then encourage a manual refresh of the list. Don't fake a progress bar.

These scores feed PickScore, so a refresh should nudge the admin to regenerate scores afterwards.

### YouTube review ingestion

Finds real reviewer opinions and attaches them to laptops so the chatbot can quote them.

| Method | Endpoint | What it does | Notes |
|---|---|---|---|
| `POST` | `/reviews/channels` | Add a YouTube channel to trust | |
| `GET` | `/reviews/channels` | List trusted channels | |
| `PATCH` | `/reviews/channels/{id}` | Edit / deactivate a channel | |
| `POST` | `/reviews/ingest/{laptop_id}` | Find videos for one laptop | 💸 |
| `POST` | `/reviews/ingest-bulk` | Find videos across the catalog. `limit` 1–20, default 5; `skip_covered` default true | 💸 |
| `GET` | `/reviews/raw` | Browse found videos (filter by `status`) | |
| `PATCH` | `/reviews/raw/{id}/match` | Manually attach a video to the right laptop | |
| `POST` | `/reviews/rematch` | Re-run automatic matching on unmatched videos | |
| `POST` | `/reviews/process/{id}` | Summarize one video's transcript | 💸 |
| `POST` | `/reviews/process-bulk` | Summarize a batch. `limit` 1–50, default 5 | ⏳💸 |
| `POST` | `/reviews/aggregate/{laptop_id}` | Roll summaries into top strengths/weaknesses | |

This is a **multi-step workflow, not one button**, and the steps are manual on purpose:

```
Add channels ──► Find videos ──► Check matches ──► Summarize ──► Aggregate
                                  (fix wrong ones by hand)
```

**Video match states:** `pending` (needs a human decision) · `matched` (attached) · `rejected` (no match found). The `pending` list is a genuine review queue — design it as one, with the video title beside the guessed laptop and a quick confirm/correct/reject action.

**Two hard limits worth surfacing:**
- Finding videos burns a **daily YouTube allowance of ~9 searches**. The `limit` (max 20) can exceed what the day allows. Show remaining budget, or at minimum warn that a big number will fail partway.
- Summarizing is slow: **a single video can take a minute or more**. A default batch of 5 is several minutes of waiting (⏳).

`skip_covered` (default on) means "don't re-check laptops that already have reviews" — that's what makes a daily run walk through the catalog instead of redoing the same laptops. Present it as a plain checkbox: *"Skip laptops that already have reviews."*

### Users

| Method | Endpoint | What it does |
|---|---|---|
| `GET` | `/users` | Browse users — `search`, `role`, `status`, `sort_by`, `sort_dir`, paginated |
| `GET` | `/users/{id}` | One user's details |
| `PATCH` | `/users/{id}/role` | 🔴 Set role: `user` or `admin` |
| `PATCH` | `/users/{id}/status` | 🔴 Set status: `active`, `inactive`, `suspended` |

Both changes take effect on the user's **next request** — a suspended user is locked out almost immediately, and a promoted user gains full destructive powers. These deserve real confirmation dialogs naming the person.

**Lockout guards (added 2026-08-06).** The backend now refuses changes that would lock everyone out:

| Attempt | Response | Message |
|---|---|---|
| Demoting or deactivating **yourself** | `403` | *Admins cannot demote or deactivate their own account.* |
| Demoting or deactivating the **last active admin** | `400` | *Cannot modify or delete the last remaining active admin account.* |

Promotions, re-activations, and re-affirming an existing admin role are never blocked.

> **Design ahead of the error.** These messages are correct but reactive. Better: disable the demote/deactivate controls on your own row with a tooltip explaining why, and treat the 4xx as the backstop. Handle both codes — 403 for self, 400 for last-admin — and show the returned `detail` text directly; it is written to be read by a person.

There is **no delete-user endpoint** at all. If the design calls for one, it needs to be built (the backend guard for it is already written).

### Agent monitoring (chatbot quality)

| Method | Endpoint | What it does |
|---|---|---|
| `GET` | `/agent/monitoring/stats` | Headline numbers — volume, success rate, speed |
| `GET` | `/agent/monitoring/runs` | Every chatbot turn — `search` (by user message), `status` (`success`/`error`), `sort_by` (`created_at`, `latency_ms`), paginated |
| `GET` | `/agent/monitoring/runs/{id}` | One conversation turn in full: what the user asked, what the bot replied, which tools it used, how long each took, any error |

Read-only, and the most naturally *dashboard-shaped* part of the portal: stats on top, filterable list below, detail drawer on click. The detail view is a debugging tool — the tool-by-tool timing breakdown is what explains a slow or wrong answer.

---

## 9. Long-running operations — now polled background jobs

*Updated 2026-08-06. This section previously described a blocking-request problem; the backend has since been changed, so the design guidance is different.*

Four actions no longer hold the connection open. They return **immediately** with a job to watch:

| Action | Endpoint | Realistic duration |
|---|---|---|
| Process scraped laptops | `POST /processor/process-pending` | ~5 s per record (~8 min for 100) |
| Add missing tags | `POST /processor/categorize-untagged` | ~5 s per laptop |
| Bulk scrape a brand | `POST /scraper/bulk-scrape` | ~12 s per page (Acer is near-instant) |
| Scrape selected targets | `POST /scraper/scrape-targets` | ~12 s per page |

### The pattern to design

**1. Start it.** The response is `202 Accepted`:

```jsonc
{
  "job_id": "36257737-…",
  "job_type": "processor.process_pending",
  "status": "queued",
  "total_count": 38,
  "estimated_seconds": 195,
  "estimated_completion_at": "2026-08-06T09:14:22Z",
  "poll_url": "/api/v2/jobs/36257737-…",
  "message": "Processing 38 pending record(s) in the background."
}
```

`total_count` is the **real** queue size, not the requested limit — show it back ("38 of your requested 100 were actually pending"). `estimated_seconds` and `estimated_completion_at` are ready-made for a countdown.

**2. Poll it.** `GET /jobs/{job_id}` every few seconds:

```jsonc
{
  "status": "processing",
  "total_count": 38, "processed_count": 19,
  "succeeded_count": 17, "failed_count": 2,
  "progress_percentage": 50.0,
  "errors": [{ "item": "Acer Swift Go 16", "error": "…" }],
  "result": null,
  "started_at": "…", "finished_at": null
}
```

Stop when `status` is `completed` or `failed`. A finished job always reads `100.0`, so the bar never sticks. `errors[]` fills **while the job runs** — a partially-failing run is visible immediately, not only at the end.

**3. Show the result.** On completion, `result` holds the full report (per-URL outcomes, `log_file`, totals).

### What this changes for the UI

- **Real progress bars are now possible** — use `progress_percentage`, with `succeeded_count` / `failed_count` beside it.
- **Navigating away is safe.** The job keeps running server-side. Design for leaving and coming back: `GET /jobs?active_only=true` powers a global "1 job running" indicator in the header, and `GET /jobs` is a job history screen.
- **`failed` ≠ items failed.** `status: "failed"` means the *run itself* crashed. A run where every item failed is still `completed`. **Judge success by `failed_count`, not by `status`** — this is the easiest thing to get wrong.
- **Large batches are now genuinely usable.** The 1,500 ceiling no longer risks a timeout. Still show the estimate, because 1,500 items is over two hours of real work.
- **Interrupted jobs self-report.** If the server restarts mid-run, the job flips to `failed` with an explanatory `error_message` — surface that text; it tells the admin to simply re-run, since completed items are not repeated.
- **There is no cancel.** Once started, a job runs to completion. Do not offer a cancel button.

> Still synchronous: `POST /reviews/process-bulk` (~1 min per video). Treat that one with the old blocking-wait guidance until it is converted.

---

## 10. Cross-cutting rules

**Response codes and how they should feel**

| Code | Meaning | Tone |
|---|---|---|
| `200` / `201` | Worked | Success, move on |
| `202` | **Accepted** — a background job started | Switch to the progress view and poll `poll_url` |
| `400` | Something about the request is wrong (e.g. brand has no website set) | Explain what to fix, in the user's words |
| `403` | Not an admin | Hide, don't explain |
| `404` | Doesn't exist | Probably deleted elsewhere — offer to refresh |
| `409` | Blocked by something still using it, or a duplicate | Name the blocker; offer a way to see it |
| `500` / `503` | Backend or AI service failed | Offer retry; failures here are often transient |

**Status vocabularies needing visual design**

| Where | Values |
|---|---|
| Background job | `queued` · `processing` · `completed` · `failed` |
| Scrape queue | `pending` · `html_uploaded` · `parsed` · `completed` · `failed` · `skipped` |
| Raw collected data | `pending` · `processing` · `completed` · `failed` |
| Found videos | `pending` · `matched` · `rejected` |
| User role | `user` · `admin` |
| User status | `active` · `inactive` · `suspended` |
| Chatbot turn | `success` · `error` |
| Uploaded file result | `matched` · `unmatched` · `invalid` |

**List endpoints share one shape.** Paginated lists (`/users`, `/agent/monitoring/runs`) take `skip` and `limit` (default 50, max 1000) and return `{ items, total, skip, limit }` — one pagination component works everywhere. Some older lists use `offset`/`limit` instead; worth confirming per screen.

**Recurring principles**

- **Failure is routine, not exceptional.** Websites change, videos go missing, AI quotas run out. Failed items are retried automatically on the next run. Design failure as a normal, calm state.
- **Re-running is safe** almost everywhere — processing skips what's done, uploads overwrite, scores recalculate. Say so; it removes hesitation.
- **Deletes are unguarded.** There is no undo and no trash. Confirmation dialogs should name the thing being deleted and, where relevant, what depends on it.
- **Nothing here is real-time.** No websockets, no push. Any "live" feel comes from polling or a manual refresh.

---

## 11. Complete endpoint index (64)

<details>
<summary>All admin endpoints grouped by area</summary>

**Jobs (2)**
`GET /jobs` · `GET /jobs/{job_id}`

**Scraper (8)**
`POST /scraper/feed-crawler` · `POST /scraper/scrape-url` · `POST /scraper/bulk-scrape` · `POST /scraper/scrape-targets` · `POST /scraper/upload-html` · `POST /scraper/upload-html/json` · `GET /scraper/targets` · `GET /scraper/raw-laptop/{id}`

**Processor (3)**
`POST /processor/process/{raw_laptop_id}` · `POST /processor/process-pending` · `POST /processor/categorize-untagged`

**Embeddings & scores (3)**
`POST /embeddings/laptops/generate-all` · `POST /embeddings/laptops/{laptop_id}` · `POST /laptops/pick-scores/generate-all`

**Laptops (4)**
`POST /laptops/` · `PUT /laptops/{id}` · `DELETE /laptops/{id}` · `GET /laptops/raw-scrap-laptops`

**Brands (3)**
`POST /brands` · `PUT /brands/{id}` · `DELETE /brands/{id}`

**Customizations (6)**
`POST /customizations/` · `POST /customizations/bulk-by-pattern` · `GET /customizations/laptop/{laptop_id}` · `GET /customizations/laptops-summary` · `PATCH /customizations/{id}` · `DELETE /customizations/{id}`

**Taxonomy (6)**
`POST /product-types` · `PUT /product-types/{id}` · `DELETE /product-types/{id}` · `POST /categories` · `PUT /categories/{id}` · `DELETE /categories/{id}`

**Questionnaire (3)**
`POST /questionnaire` · `PUT /questionnaire/{id}` · `DELETE /questionnaire/{id}`

**Benchmarks (10)**
`POST /benchmarks/scrape/cpu` · `POST /benchmarks/scrape/gpu` · `POST /benchmarks/cpu` · `POST /benchmarks/gpu` · `PATCH /benchmarks/cpu/{id}` · `PATCH /benchmarks/gpu/{id}` · `DELETE /benchmarks/cpu/{id}` · `DELETE /benchmarks/gpu/{id}` *(+ public GET list/detail for both)*

**Reviews (11)**
`POST /reviews/channels` · `GET /reviews/channels` · `PATCH /reviews/channels/{id}` · `POST /reviews/ingest/{laptop_id}` · `POST /reviews/ingest-bulk` · `GET /reviews/raw` · `PATCH /reviews/raw/{id}/match` · `POST /reviews/rematch` · `POST /reviews/process/{id}` · `POST /reviews/process-bulk` · `POST /reviews/aggregate/{laptop_id}`

**Users (4)**
`GET /users` · `GET /users/{id}` · `PATCH /users/{id}/role` · `PATCH /users/{id}/status`

**Agent monitoring (3)**
`GET /agent/monitoring/stats` · `GET /agent/monitoring/runs` · `GET /agent/monitoring/runs/{id}`

</details>

---

## 12. Suggested screen inventory

A starting point, ordered by how central each screen is:

| Priority | Screen | Built from |
|---|---|---|
| 1 | **Pipeline dashboard** — counts stuck at each stage, each one a call to action | Queue counts, embedding status, monitoring stats |
| 2 | **Scrape queue** — filterable table, multi-select, run actions, result summary | `/scraper/targets` + the scrape actions |
| 3 | **Acer HTML upload** — drag-drop, per-file results, outstanding to-do list | `/scraper/upload-html` + failed targets |
| 4 | **Processing** — batch size, time estimate, result summary | `/processor/*` + `/jobs/{id}` |
| 4b | **Job progress** — a shared component (progress bar, live error list, result panel) reused by every 🔄 action, plus a header "job running" indicator from `/jobs?active_only=true` | `/jobs/*` |
| 5 | **Laptop catalog** — list, 9-section editor, delete | `/laptops/*` |
| 6 | **Review queue** — pending video matches, confirm/correct/reject | `/reviews/raw` + match |
| 7 | **Chatbot monitoring** — stats, run list, detail drawer | `/agent/monitoring/*` |
| 8 | **Users** — search, role/status changes | `/users/*` |
| 9 | **Settings** — brands, taxonomy, questionnaire, benchmarks | remaining CRUD |

---

*Backend reference: [CLAUDE.md](CLAUDE.md) (architecture, design decisions) and [Progress.md](Progress.md) (full endpoint tables including public and customer-facing routes).*
