from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.database import get_session
from pydantic import BaseModel
from uuid import UUID

from app.laptops.models import RawScrapLaptop
from app.users.auth import get_current_admin
from .apple_scraper import crawl_apple_specs_links, scrape_official_website
from app.scraper.models import ScrapeTarget

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScraperRequest(BaseModel):
    url: str
    brand_id: UUID


class CrawlerQueueRequest(BaseModel):
    start_url: str
    brand_id: UUID


@router.post("/feed-crawler", dependencies=[Depends(get_current_admin)])
def feed_crawler_queue(
    request: CrawlerQueueRequest, session: Session = Depends(get_session)
) -> dict:
    from app.laptops.models import LaptopBrand

    # Verify the brand_id exists
    brand = session.get(LaptopBrand, request.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    # Only Apple brand scraping is currently supported
    if brand.name.lower() != "apple":
        raise HTTPException(
            status_code=400, detail="Currently, only Apple brand scraping is supported."
        )

    found_links = crawl_apple_specs_links(request.start_url)

    if not found_links:
        return {"message": "Crawler completed, but no valid links were found."}

    results_summary = {"newly_added": 0, "already_in_queue": 0}

    for link in found_links:
        existing_target = session.exec(
            select(ScrapeTarget).where(ScrapeTarget.url == link)
        ).first()

        if existing_target:
            results_summary["already_in_queue"] += 1
        else:
            new_target = ScrapeTarget(
                url=link,
                brand_id=request.brand_id,
                is_active=True,  # type: ignore
            )
            session.add(new_target)
            results_summary["newly_added"] += 1

    session.commit()

    return {
        "message": f"Crawler task completed! Found {len(found_links)} total links.",
        "summary": results_summary,
    }


@router.post("/run", dependencies=[Depends(get_current_admin)])
def run_official_scraper(
    request: ScraperRequest, session: Session = Depends(get_session)
) -> dict:
    from app.laptops.models import LaptopBrand

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

    result = scrape_official_website(request.url, brand.name, request.brand_id)  # type: ignore

    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail=result.get("error"))

    raw_laptop = RawScrapLaptop(
        source_url=request.url,
        brand_id=request.brand_id,
        raw_product_name=result["product_name"],
        raw_prices=result.get("raw_prices_list", []),
        image_urls=result.get("image_urls", []),
        raw_specs_dump={"scraped_features": result.get("raw_specs", [])},
        processing_status="pending",
    )

    session.add(raw_laptop)
    session.commit()
    session.refresh(raw_laptop)

    return {
        "message": "Scraped and saved to staging queue successfully!",
        "raw_id": raw_laptop.id,
        "raw_data": result,
    }
