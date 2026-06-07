
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.database import get_session
from pydantic import BaseModel

from app.laptops.models import RawScrapLaptop 
from app.users.auth import get_current_admin
from .engine import scrape_official_website

router = APIRouter(prefix="/scraper", tags=["scraper"])

class ScraperRequest(BaseModel):
    url: str
    brand: str

@router.post("/run", dependencies=[Depends(get_current_admin)])
def run_official_scraper(
    request: ScraperRequest, 
    session: Session = Depends(get_session)
) -> dict:
    
    # Prevent duplicate scraping
    existing_scrape = session.exec(
        select(RawScrapLaptop).where(RawScrapLaptop.source_url == request.url)
    ).first()
    
    if existing_scrape:
        return {"message": "URL already scraped.", "status": existing_scrape.processing_status}

    # Run the Playwright engine
    result = scrape_official_website(request.url, request.brand)
    
    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail=result.get('error'))

    # Dump into the Staging Table
    raw_laptop = RawScrapLaptop(
        source_url=request.url,
        brand=result["brand"],
        raw_product_name=result["product_name"],
        raw_price_rm=result["price_rm"],
        image_url=result.get("image_url"),
        raw_specs_dump={"scraped_features": result.get("raw_specs", [])},
        processing_status="pending"
    )

    session.add(raw_laptop)
    session.commit()
    session.refresh(raw_laptop)
    
    return {
        "message": "Scraped and saved to staging queue successfully!", 
        "raw_id": raw_laptop.id
    }