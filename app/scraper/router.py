from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select
from app.database import get_session
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime, timezone

from app.laptops.brand_model import LaptopBrand
from app.users.auth import get_current_admin
from .apple_scraper import crawl_apple_specs_links, scrape_official_website
from .asus_scraper import crawl_asus_specs_links, scrape_asus_laptop_specs
from app.scraper.models import ScrapeTarget, RawScrapLaptop
from .bulk_scraper import run_bulk_scrape

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScraperRequest(BaseModel):
    url: str
    brand_id: UUID


class CrawlerQueueRequest(BaseModel):
    start_url: str
    brand_id: UUID


class BulkScrapeRequest(BaseModel):
    brand_id: UUID


@router.post("/feed-crawler", dependencies=[Depends(get_current_admin)])
async def feed_crawler_queue(
    request: CrawlerQueueRequest, session: Session = Depends(get_session)
) -> dict:

    # Verify the brand_id exists
    brand = session.get(LaptopBrand, request.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    found_urls = []

    # 1. Route based on Brand
    if brand.name.lower() == "apple":
        # apple_scraper is now async (see playwright_utils refactor)
        found_urls = await crawl_apple_specs_links(request.start_url)
    
    elif brand.name.lower() == "asus":
        
        # Playwright is async, so we await it
        found_urls = await crawl_asus_specs_links(request.start_url)
    
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

    else:
        raise HTTPException(
            status_code=400, detail=f"Currently, {brand.name} brand scraping is not supported."
        )

    # 3. Check if every variant failed
    all_failed = all(v.get("status") == "failed" for v in variant_results)

    # Stamp last_scraped_at and scrape_status regardless of outcome
    outcome_status = "failed" if all_failed else "completed"
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
# Bulk Scrape
# ---------------------------------------------------------------------------

@router.post("/bulk-scrape", dependencies=[Depends(get_current_admin)])
async def bulk_scrape(
    request: BulkScrapeRequest,
    response: Response,
    session: Session = Depends(get_session),
):
    """
    Scrape all pending URLs (last_scraped_at IS NULL) for the given brand.

    Returns:
    - HTTP 200 when every URL succeeded (or there were no pending URLs).
    - HTTP 207 Multi-Status when at least one URL failed.

    A timestamped failure log is written to logs/scraper/ whenever any URL
    fails, containing the URL and its error message.
    """
    try:
        report = await run_bulk_scrape(brand_id=request.brand_id, session=session)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Serialise the dataclass into a plain dict for JSON output
    payload = {
        "brand": report.brand_name,
        "total_pending": report.total_pending,
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

    # HTTP 207 Multi-Status when there is at least one failure
    if report.failed > 0:
        response.status_code = 207

    return payload


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