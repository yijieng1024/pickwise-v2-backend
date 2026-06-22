from fastapi import APIRouter, Depends, HTTPException
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

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScraperRequest(BaseModel):
    url: str
    brand_id: UUID


class CrawlerQueueRequest(BaseModel):
    start_url: str
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

    # Prevent duplicate scraping
    existing_scrape = session.exec(
        select(RawScrapLaptop).where(RawScrapLaptop.source_url == request.url)
    ).first()

    if existing_scrape:
        return {
            "message": "URL already scraped.",
            "status": existing_scrape.processing_status,
        }

    # 1. Route based on Brand
    if brand.name.lower() == "apple":
        # apple_scraper is now async (see playwright_utils refactor)
        result = await scrape_official_website(request.url, brand.name, request.brand_id) # type: ignore

    elif brand.name.lower() == "asus":
        result = await scrape_asus_laptop_specs(request.url, request.brand_id)

    else:
        raise HTTPException(
            status_code=400, detail=f"Currently, {brand.name} brand scraping is not supported."
        )

    # 2. Always stamp last_scraped_at — regardless of success or failure.
    #    If the URL was never fed through /feed-crawler, create the ScrapeTarget
    #    row so the timestamp is never silently lost.
    scrape_target = session.exec(
        select(ScrapeTarget).where(ScrapeTarget.url == request.url)
    ).first()

    if scrape_target:
        scrape_target.last_scraped_at = datetime.now(timezone.utc)
        session.merge(scrape_target)
    else:
        # URL was scraped directly without going through the crawler queue
        new_target = ScrapeTarget(
            url=request.url,
            brand_id=request.brand_id,
            last_scraped_at=datetime.now(timezone.utc),
        )
        session.add(new_target)

    if result.get("status") == "failed":
        # Commit the timestamp update even on failure so we don't lose the record
        session.commit()
        raise HTTPException(status_code=500, detail=result.get("error"))

    # 3. Save the successfully scraped data
    raw_laptop = RawScrapLaptop(
        source_url=request.url,
        brand_id=request.brand_id,
        raw_product_name=result.get("product_name", "Unknown Model"),
        raw_prices=result.get("raw_prices_list", []),
        image_urls=result.get("image_urls", []),
        raw_specs_dump={"scraped_features": result.get("raw_specs", [])},
        processing_status="pending",
    )
    session.add(raw_laptop)
    session.commit()
    session.refresh(raw_laptop)

    return {
        "message": f"Successfully scraped {brand.name} laptop data.",
        "laptop_id": raw_laptop.id,
        "last_scraped_at": datetime.now(timezone.utc).isoformat(),
    }