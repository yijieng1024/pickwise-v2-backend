# app/processor/router.py
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select
from typing import List, Dict, Any

from app.database import get_session 
from app.laptops.laptop_models import RawScrapLaptop
from app.processor.engine import process_raw_laptop_data
from app.users.auth import get_current_admin

router = APIRouter(prefix="/processor", tags=["Processor"])

@router.post("/process/{raw_laptop_id}", dependencies=[Depends(get_current_admin)])
def process_single_laptop(
    raw_laptop_id: str, 
    session: Session = Depends(get_session)
) -> Dict[str, Any]:
    """
    Manually trigger the AI processor for a single raw scraped laptop.
    """
    result = process_raw_laptop_data(raw_laptop_id, session)
    
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))
        
    return result

@router.post("/process-pending", dependencies=[Depends(get_current_admin)])
def process_all_pending_laptops(
    session: Session = Depends(get_session)
) -> Dict[str, Any]:
    """
    Sweeps the RawScrapLaptop table for any entries with status='pending'
    and processes them sequentially through the LLM.
    """
    # 1. Find all pending records
    pending_records = session.exec(
        select(RawScrapLaptop).where(RawScrapLaptop.processing_status == "pending")
    ).all()

    if not pending_records:
        return {"message": "No pending records found in the queue."}

    results_summary = []
    total_variants_saved = 0

    # 2. Process each one
    for record in pending_records:
        res = process_raw_laptop_data(str(record.id), session)
        
        # Keep track of what happened for the API response
        results_summary.append({
            "raw_id": str(record.id),
            "product_name": record.raw_product_name,
            "status": res.get("status"),
            "variants_extracted": res.get("variants_extracted", 0)
        })
        
        if res.get("status") == "success":
            total_variants_saved += res.get("variants_saved", 0)

    return {
        "message": f"Bulk processing complete. Processed {len(pending_records)} raw records.",
        "total_new_variants_saved": total_variants_saved,
        "details": results_summary
    }