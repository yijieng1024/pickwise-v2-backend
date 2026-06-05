# app/scraper/router.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Dict, Any

from app.users.auth import get_current_admin
from .engine import scrape_official_website

router = APIRouter(prefix="/scraper", tags=["scraper"])

class ScraperRequest(BaseModel):
    url: str
    brand: str 

@router.post("/run", dependencies=[Depends(get_current_admin)])
def run_official_scraper(request: ScraperRequest) -> Dict[str, Any]:
    valid_brands = ["apple", "lenovo"]
    if request.brand.lower() not in valid_brands:
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported brand. Currently supported: {', '.join(valid_brands)}"
        )

    # Remove the 'await' here
    result = scrape_official_website(request.url, request.brand)
    
    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail=f"Scraping failed: {result.get('error')}")
        
    return {"message": "Scraping successful", "data": result}