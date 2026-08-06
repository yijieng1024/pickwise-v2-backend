from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, or_
from sqlmodel import Session, select
from app.database import get_session
from pydantic import BaseModel, Field
from typing import List, Optional
from uuid import UUID
from datetime import datetime, timezone

from app.laptops.brand_model import LaptopBrand
from app.users.auth import get_current_admin
from .apple_scraper import crawl_apple_specs_links, scrape_official_website
from .asus_scraper import crawl_asus_specs_links, scrape_asus_laptop_specs
from .acer_scraper import crawl_acer_specs_links, scrape_acer_laptop_specs
from app.scraper.models import ScrapeStatus, ScrapeTarget, RawScrapLaptop
from app.scraper.html_ingest import (
    MAX_HTML_BYTES,
    HtmlIngestError,
    decode_html,
    extract_canonical_url,
    get_product_type,
    resolve_target,
    store_html,
)
from app.scraper.raw_html_model import (
    RawHtmlItemResult,
    RawHtmlUploadRequest,
    RawHtmlUploadSummary,
)
from app.common.job_model import JobAccepted, JobType
from app.common.job_service import JobProgress, create_job, job_accepted, run_job
from app.users.models import User
from .bulk_scraper import _success_status, run_bulk_scrape, run_scrape_for_targets

router = APIRouter(prefix="/scraper", tags=["Scraper"])

# Rough per-target cost for the ETA shown to the admin. Live Playwright scrapes
# dominate this; Acer targets are parsed from stored HTML and are near-instant,
# so a brand-wide Acer run finishes well ahead of its estimate.
_SECONDS_PER_TARGET = 12


class ScraperRequest(BaseModel):
    url: str
    brand_id: UUID


class CrawlerQueueRequest(BaseModel):
    brand_id: UUID


class BulkScrapeRequest(BaseModel):
    brand_id: UUID


class TargetScrapeRequest(BaseModel):
    # min_length=1 so an empty selection is a 422 from FastAPI rather than
    # reaching the service layer and surfacing as a misleading 404
    target_ids: List[UUID] = Field(..., min_length=1)


@router.post("/feed-crawler", dependencies=[Depends(get_current_admin)])
async def feed_crawler_queue(
    request: CrawlerQueueRequest, session: Session = Depends(get_session)
) -> dict:
    """
    Crawl a brand's official website and queue every laptop it finds.

    The URL crawled is the brand's own `base_scrape_url` from `laptop_brands` —
    it is not accepted from the client, so a brand can never be crawled against
    another brand's site.
    """
    # Verify the brand_id exists
    brand = session.get(LaptopBrand, request.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    start_url = (brand.base_scrape_url or "").strip()
    if not start_url:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Brand '{brand.name}' has no base_scrape_url set. "
                "Update the brand before crawling."
            ),
        )

    found_urls = []

    # 1. Route based on Brand
    if brand.name.lower() == "apple":
        # apple_scraper is now async (see playwright_utils refactor)
        found_urls = await crawl_apple_specs_links(start_url)
    
    elif brand.name.lower() == "asus":

        # Playwright is async, so we await it
        found_urls = await crawl_asus_specs_links(start_url)

    elif brand.name.lower() == "acer":
        # Lists the pages stored in raw_product_htmls — this brand is parsed
        # from uploaded HTML (the store's WAF refuses automated requests)
        found_urls = await crawl_acer_specs_links(start_url, session, brand.id)

    else:
        raise HTTPException(
            status_code=400, detail=f"Currently, {brand.name} brand crawling is not supported."
        )

    if not found_urls:
        return {"message": "No URLs found to add to the queue.", "added_count": 0}

    # 2. Add found URLs to the laptop_scrape_urls table (ScrapeTarget)
    added_count = 0
    for url in found_urls:
        existing = session.exec(
            select(ScrapeTarget).where(ScrapeTarget.url == url)
        ).first()

        if not existing:
            new_target = ScrapeTarget(url=url, brand_id=brand.id)
            session.add(new_target)
            added_count += 1

    session.commit()

    return {
        "message": f"Successfully processed {brand.name} crawler queue.",
        "total_found": len(found_urls),
        "added_to_queue": added_count,
    }


@router.post("/scrape-url", dependencies=[Depends(get_current_admin)])
async def scrape_url(
    request: ScraperRequest, session: Session = Depends(get_session)
) -> dict:
    from app.laptops.brand_model import LaptopBrand

    # Verify the brand_id exists and get the brand name
    brand = session.get(LaptopBrand, request.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    # Prevent duplicate scraping — check for bare URL or any variant (?v=N)
    existing_scrape = session.exec(
        select(RawScrapLaptop).where(
            RawScrapLaptop.source_url.like(f"{request.url}%")  # type: ignore[arg-type]
        )
    ).first()

    if existing_scrape:
        return {
            "message": "URL already scraped.",
            "status": existing_scrape.processing_status,
        }

    # 1. Route based on Brand — ASUS returns list[dict], Apple returns dict (wrapped below)
    if brand.name.lower() == "apple":
        raw_result = await scrape_official_website(request.url, brand.name, request.brand_id)  # type: ignore
        variant_results = [raw_result]

    elif brand.name.lower() == "asus":
        # Returns list[dict] — one item per variant found on the page
        variant_results = await scrape_asus_laptop_specs(request.url, request.brand_id)

    elif brand.name.lower() == "acer":
        # Returns list[dict] — always exactly one (simple products). Reads the
        # page from raw_product_htmls, so it must have been uploaded first.
        variant_results = await scrape_acer_laptop_specs(
            request.url, request.brand_id, session
        )

    else:
        raise HTTPException(
            status_code=400, detail=f"Currently, {brand.name} brand scraping is not supported."
        )

    # 3. Check if every variant failed
    all_failed = all(v.get("status") == "failed" for v in variant_results)

    # Stamp last_scraped_at and scrape_status regardless of outcome
    outcome_status = (
        ScrapeStatus.FAILED if all_failed else _success_status(brand.name)
    )
    scrape_target = session.exec(
        select(ScrapeTarget).where(ScrapeTarget.url == request.url)
    ).first()

    if scrape_target:
        scrape_target.last_scraped_at = datetime.now(timezone.utc)
        scrape_target.scrape_status = outcome_status
        session.merge(scrape_target)
    else:
        new_target = ScrapeTarget(
            url=request.url,
            brand_id=request.brand_id,
            last_scraped_at=datetime.now(timezone.utc),
            scrape_status=outcome_status,
        )
        session.add(new_target)

    session.commit()

    if all_failed:
        first_error = variant_results[0].get("error", "Unknown scraper error")
        raise HTTPException(status_code=500, detail=first_error)

    # 4. Save one RawScrapLaptop row per successful variant
    saved_ids = []
    for variant in variant_results:
        if variant.get("status") == "failed":
            continue

        suffix = variant.get("source_url_suffix", "")
        source_url = f"{request.url}{suffix}"

        raw_laptop = RawScrapLaptop(
            source_url=source_url,
            brand_id=request.brand_id,
            raw_product_name=variant.get("product_name", "Unknown Model"),
            raw_prices=variant.get("raw_prices_list", []),
            image_urls=variant.get("image_urls", []),
            raw_specs_dump={"scraped_features": variant.get("raw_specs", [])},
            processing_status="pending",
        )
        session.add(raw_laptop)
        session.commit()
        session.refresh(raw_laptop)
        saved_ids.append(str(raw_laptop.id))

    return {
        "message": f"Successfully scraped {brand.name} laptop data.",
        "variants_saved": len(saved_ids),
        "laptop_ids": saved_ids,
        "last_scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Raw HTML upload — for brands whose pages cannot be fetched automatically
# ---------------------------------------------------------------------------


def _ingest_documents(
    session: Session,
    documents: list[tuple[str, str, Optional[str]]],
    product_type_name: str,
    brand_id: Optional[UUID],
) -> RawHtmlUploadSummary:
    """
    Store a batch of pages.

    *documents* is (source_name, page_html, canonical_override). Each item is
    independent: one bad page is reported in the summary and never aborts the
    batch, which matters when 49 files are uploaded at once. The whole batch
    commits once at the end so a partial failure cannot leave a target on
    `html_uploaded` with no row behind it.
    """
    product_type = get_product_type(session, product_type_name)

    results: list[RawHtmlItemResult] = []
    matched = unmatched = invalid = inserted = updated = 0

    for source_name, page_html, canonical_override in documents:
        canonical_url = ""
        try:
            canonical_url = (canonical_override or "").strip() or extract_canonical_url(
                page_html
            )
            if not canonical_url:
                raise HtmlIngestError(
                    "no <link rel=\"canonical\"> or og:url — is this a product "
                    "page, or did the save capture a bot-challenge page?"
                )

            target = resolve_target(
                session, canonical_url, product_type_name, brand_id
            )
            created = store_html(
                session,
                page_html=page_html,
                product_type=product_type,
                target=target,
                canonical_url=canonical_url,
            )

            matched += 1
            inserted += 1 if created else 0
            updated += 0 if created else 1
            results.append(
                RawHtmlItemResult(
                    source_name=source_name,
                    status="matched",
                    canonical_url=canonical_url,
                    target_id=target.id,
                    created=created,
                )
            )

        except HtmlIngestError as e:
            # "no target" is an unmatched page; anything else is a bad document
            is_unmatched = "No queued scrape target" in str(e)
            unmatched += 1 if is_unmatched else 0
            invalid += 0 if is_unmatched else 1
            results.append(
                RawHtmlItemResult(
                    source_name=source_name,
                    status="unmatched" if is_unmatched else "invalid",
                    canonical_url=canonical_url or None,
                    error=str(e),
                )
            )

    session.commit()

    return RawHtmlUploadSummary(
        received=len(documents),
        matched=matched,
        unmatched=unmatched,
        invalid=invalid,
        inserted=inserted,
        updated=updated,
        results=results,
    )


@router.post("/upload-html", dependencies=[Depends(get_current_admin)])
async def upload_raw_html(
    files: List[UploadFile] = File(..., description="One or more saved .html pages"),
    product_type: str = Form("laptop", description="Product line, e.g. 'laptop'"),
    brand_id: Optional[UUID] = Form(
        None, description="Restrict matching to one brand (recommended)"
    ),
    session: Session = Depends(get_session),
) -> RawHtmlUploadSummary:
    """
    Upload saved product pages (multipart/form-data) into `raw_product_htmls`.

    For brands that cannot be scraped live — today Acer, whose store answers
    automated requests with a WAF block. Save the page in a normal browser
    ("Webpage, HTML Only") and post it here.

    Each file is identified by its own `<link rel="canonical">` tag, so
    **filenames are irrelevant**. That URL is matched back to a queued row in
    `laptop_scrape_urls`, the HTML is upserted, and the target moves to
    `html_uploaded` — ready for POST /scraper/bulk-scrape to parse.

    Re-uploading a page overwrites the stored HTML and re-queues its target,
    which is how a stale price is refreshed.
    """
    documents: list[tuple[str, str, Optional[str]]] = []
    oversized: list[RawHtmlItemResult] = []

    for upload in files:
        name = upload.filename or "unnamed"
        raw = await upload.read()

        if len(raw) > MAX_HTML_BYTES:
            oversized.append(
                RawHtmlItemResult(
                    source_name=name,
                    status="invalid",
                    error=(
                        f"{len(raw)} bytes exceeds the {MAX_HTML_BYTES}-byte limit "
                        "— upload the HTML only, not a full page archive."
                    ),
                )
            )
            continue

        if not raw.strip():
            oversized.append(
                RawHtmlItemResult(
                    source_name=name, status="invalid", error="file is empty"
                )
            )
            continue

        documents.append((name, decode_html(raw), None))

    try:
        summary = _ingest_documents(session, documents, product_type, brand_id)
    except HtmlIngestError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Fold in the files rejected before parsing
    if oversized:
        summary.received += len(oversized)
        summary.invalid += len(oversized)
        summary.results.extend(oversized)

    return summary


@router.post("/upload-html/json", dependencies=[Depends(get_current_admin)])
def upload_raw_html_json(
    request: RawHtmlUploadRequest,
    product_type: str = Query("laptop", description="Product line, e.g. 'laptop'"),
    brand_id: Optional[UUID] = Query(
        None, description="Restrict matching to one brand (recommended)"
    ),
    session: Session = Depends(get_session),
) -> RawHtmlUploadSummary:
    """
    JSON twin of POST /scraper/upload-html, for callers that already hold the
    HTML as a string (a browser extension, a scripted importer).

    Same matching and upsert rules; `canonical_url` may be supplied per document
    to override a page whose canonical tag is missing or wrong.
    """
    documents = [
        (doc.source_name or doc.canonical_url or f"document[{i}]", doc.raw_html, doc.canonical_url)
        for i, doc in enumerate(request.documents)
    ]

    if not documents:
        raise HTTPException(status_code=400, detail="No documents provided.")

    try:
        return _ingest_documents(session, documents, product_type, brand_id)
    except HtmlIngestError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Crawled queue — listing for the admin selection UI
# ---------------------------------------------------------------------------

@router.get("/targets/status-counts", dependencies=[Depends(get_current_admin)])
def scrape_target_status_counts(
    session: Session = Depends(get_session),
    brand_id: Optional[UUID] = Query(None, description="Restrict the counts to one brand"),
) -> dict:
    """
    One count per scrape_status, plus the total.

    The queue UI shows a tab per status. Getting these from GET /targets meant
    six requests, each of which materialises every matching row to compute
    `total` — this answers all six in a single grouped query.

    Statuses with no rows are still returned as 0 so the tabs never disappear.
    """
    statement = select(ScrapeTarget.scrape_status, func.count()).group_by(
        ScrapeTarget.scrape_status
    )
    if brand_id is not None:
        statement = statement.where(ScrapeTarget.brand_id == brand_id)

    counts = {
        "pending": 0,
        "html_uploaded": 0,
        "parsed": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
    }
    total = 0
    for status_value, count in session.execute(statement).all():
        total += count
        if status_value in counts:
            counts[status_value] = count

    return {**counts, "total": total}


@router.get("/targets", dependencies=[Depends(get_current_admin)])
def list_scrape_targets(
    session: Session = Depends(get_session),
    brand_id: Optional[UUID] = Query(None, description="Filter by brand"),
    scrape_status: Optional[str] = Query(
        None,
        description=(
            "pending | html_uploaded | parsed | completed | failed | skipped. "
            "`failed` surfaces targets whose HTML is missing or unparseable; "
            "`html_uploaded` are uploaded but not yet parsed."
        ),
    ),
    is_active: Optional[bool] = Query(None, description="Filter by active flag"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """
    List the crawled laptop URLs in `laptop_scrape_urls`.

    This is what the admin UI reads to show what the crawler found, so
    individual laptops can be selected and scraped via POST /scraper/scrape-targets.
    """
    filters = []
    if brand_id is not None:
        filters.append(ScrapeTarget.brand_id == brand_id)
    if scrape_status is not None:
        filters.append(ScrapeTarget.scrape_status == scrape_status)
    if is_active is not None:
        filters.append(ScrapeTarget.is_active == is_active)

    total = len(session.exec(select(ScrapeTarget).where(*filters)).all())

    targets = session.exec(
        select(ScrapeTarget)
        .where(*filters)
        .order_by(ScrapeTarget.created_at.desc())  # type: ignore[attr-defined]
        .offset(offset)
        .limit(limit)
    ).all()

    # Resolve brand names once so the UI does not need a second call
    brand_names: dict[UUID, str] = {}
    for t in targets:
        if t.brand_id not in brand_names:
            brand = session.get(LaptopBrand, t.brand_id)
            brand_names[t.brand_id] = brand.name if brand else "Unknown"

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "targets": [
            {
                "id": t.id,
                "url": t.url,
                "brand_id": t.brand_id,
                "brand_name": brand_names[t.brand_id],
                "scrape_status": t.scrape_status,
                "is_active": t.is_active,
                "last_scraped_at": t.last_scraped_at,
                "created_at": t.created_at,
            }
            for t in targets
        ],
    }


# ---------------------------------------------------------------------------
# Scrape selected laptops (admin picks rows out of the crawled queue)
# ---------------------------------------------------------------------------

def _report_payload(report, total_key: str) -> dict:
    """Serialise a BulkScrapeReport into the job's `result` payload."""
    return {
        "brand": report.brand_name,
        total_key: report.total_pending,
        "processed": report.processed,
        "succeeded": report.succeeded,
        "failed": report.failed,
        "skipped": report.skipped,
        "log_file": report.log_file,
        "results": [
            {"url": r.url, "status": r.status, "error": r.error}
            for r in report.results
        ],
    }


@router.post(
    "/scrape-targets",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def scrape_selected_targets(
    request: TargetScrapeRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
) -> JobAccepted:
    """
    Scrape the specific laptops an admin selected from the crawled queue.

    Accepts one or many `target_ids` from `laptop_scrape_urls`. The URL and
    brand are read from each target row, so no url/brand mismatch is possible.
    Unlike bulk-scrape this ignores `scrape_status` — an explicitly selected
    row is always attempted — but URLs already present in `raw_scrap_laptops`
    still report as "skipped" rather than duplicating.

    **Returns 202 immediately.** Poll `GET /api/v2/jobs/{job_id}` for progress;
    the finished job's `result` holds the full per-URL report that this
    endpoint used to return synchronously (including `log_file`). Unknown
    target ids fail the job rather than 404-ing here, since validation now
    happens inside the worker.
    """
    # Validate up front so a bad id is a 404 now rather than a failed job later
    known = session.exec(
        select(ScrapeTarget.id).where(ScrapeTarget.id.in_(request.target_ids))  # type: ignore[attr-defined]
    ).all()
    missing = [str(t) for t in request.target_ids if t not in set(known)]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"ScrapeTarget id(s) not found: {', '.join(missing)}",
        )

    job = create_job(
        session,
        job_type=JobType.SCRAPE_TARGETS,
        total_count=len(request.target_ids),
        params={"target_ids": [str(t) for t in request.target_ids]},
        created_by=current_admin.id,
        seconds_per_item=_SECONDS_PER_TARGET,
    )

    target_ids = list(request.target_ids)

    async def worker(work_session: Session, progress: JobProgress) -> dict:
        report = await run_scrape_for_targets(
            target_ids=target_ids, session=work_session, progress=progress
        )
        return _report_payload(report, "total_selected")

    background_tasks.add_task(run_job, job.id, worker)

    return job_accepted(
        job, message=f"Scraping {len(target_ids)} selected target(s) in the background."
    )


# ---------------------------------------------------------------------------
# Bulk Scrape
# ---------------------------------------------------------------------------

@router.post(
    "/bulk-scrape",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def bulk_scrape(
    request: BulkScrapeRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_admin: User = Depends(get_current_admin),
) -> JobAccepted:
    """
    Scrape every pending (and previously failed, and newly HTML-uploaded) URL
    for the given brand.

    **Returns 202 immediately** — a brand-wide run walks dozens of product
    pages and can take many minutes, far too long to hold an HTTP connection
    open. Poll `GET /api/v2/jobs/{job_id}` for live counts and per-URL errors.

    The finished job's `result` carries the full report this endpoint used to
    return synchronously — including `log_file`, the timestamped failure log
    written to logs/scraper/ whenever any URL fails. Partial failure is normal:
    check `failed_count`, not the job status, which is `completed` as long as
    the run itself finished.
    """
    brand = session.get(LaptopBrand, request.brand_id)
    if brand is None:
        raise HTTPException(
            status_code=404, detail=f"Brand with id={request.brand_id} not found."
        )

    queued = len(
        session.exec(
            select(ScrapeTarget.id).where(
                ScrapeTarget.brand_id == request.brand_id,
                ScrapeTarget.is_active == True,  # noqa: E712
                or_(
                    ScrapeTarget.last_scraped_at == None,  # noqa: E711
                    ScrapeTarget.scrape_status == ScrapeStatus.FAILED,
                    ScrapeTarget.scrape_status == ScrapeStatus.HTML_UPLOADED,
                ),
            )
        ).all()
    )

    job = create_job(
        session,
        job_type=JobType.BULK_SCRAPE,
        total_count=queued,
        params={"brand_id": str(request.brand_id), "brand_name": brand.name},
        created_by=current_admin.id,
        seconds_per_item=_SECONDS_PER_TARGET,
    )

    brand_id = request.brand_id

    async def worker(work_session: Session, progress: JobProgress) -> dict:
        report = await run_bulk_scrape(
            brand_id=brand_id, session=work_session, progress=progress
        )
        return _report_payload(report, "total_pending")

    background_tasks.add_task(run_job, job.id, worker)

    return job_accepted(
        job,
        message=(
            f"Scraping {queued} pending target(s) for {brand.name} in the background."
            if queued
            else f"No pending targets for {brand.name} — the job will finish immediately."
        ),
    )


# ---------------------------------------------------------------------------
# Raw Scraped Laptop — single record detail
# ---------------------------------------------------------------------------

@router.get("/raw-laptop/{raw_laptop_id}", dependencies=[Depends(get_current_admin)])
def get_raw_laptop(
    raw_laptop_id: UUID,
    session: Session = Depends(get_session),
):
    """
    Retrieve the full details of a single raw scraped laptop record.

    Path param:
    - **raw_laptop_id**: UUID of the RawScrapLaptop row.

    Returns all fields including raw_specs_dump, image_urls, prices,
    processing_status, and created_at.
    """
    raw_laptop = session.get(RawScrapLaptop, raw_laptop_id)

    if not raw_laptop:
        raise HTTPException(
            status_code=404,
            detail=f"Raw scraped laptop with id={raw_laptop_id} not found.",
        )

    return {
        "id": raw_laptop.id,
        "source_url": raw_laptop.source_url,
        "brand_id": raw_laptop.brand_id,
        "raw_product_name": raw_laptop.raw_product_name,
        "raw_prices": raw_laptop.raw_prices,
        "image_urls": raw_laptop.image_urls,
        "raw_specs_dump": raw_laptop.raw_specs_dump,
        "processing_status": raw_laptop.processing_status,
        "created_at": raw_laptop.created_at,
    }