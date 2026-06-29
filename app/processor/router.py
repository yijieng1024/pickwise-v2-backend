import time
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select
from typing import Dict, Any

from app.database import get_session
from app.scraper.models import RawScrapLaptop
from app.processor.engine import process_raw_laptop_data
from app.users.auth import get_current_admin
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/processor", tags=["Processor"])

# Gemma 4 31B free-tier limits (gemma-4-31b-it):
#  15 RPM  →  minimum 4 s between requests; using 5 s for safety margin
#  1500 RPD →  default batch of 100 per run (~8 min); hard cap at 1500
_INTER_REQUEST_DELAY_S = 5    # 5 s > 60/15, gives a small safety margin
_DEFAULT_BATCH_LIMIT   = 100  # sensible default per run; well under 1500 RPD
_MAX_BATCH_LIMIT       = 1500 # full daily quota ceiling
_RATE_LIMIT_KEYWORDS   = ("429", "quota", "resource exhausted", "rate limit")
_RETRY_WAIT_S          = 65   # wait just over 1 minute then retry once


@router.post("/process/{raw_laptop_id}", dependencies=[Depends(get_current_admin)])
def process_single_laptop(
    raw_laptop_id: str,
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """Manually trigger the AI processor for a single raw scraped laptop."""
    result = process_raw_laptop_data(raw_laptop_id, session)

    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))

    return result


@router.post("/process-pending", dependencies=[Depends(get_current_admin)])
def process_all_pending_laptops(
    session: Session = Depends(get_session),
    limit: int = Query(
        default=_DEFAULT_BATCH_LIMIT,
        ge=1,
        le=_MAX_BATCH_LIMIT,
        description=(
            f"Max records to process in this run (default {_DEFAULT_BATCH_LIMIT}). "
            f"Hard ceiling is {_MAX_BATCH_LIMIT} to stay within the Gemma 4 free-tier daily quota (1500 RPD)."
        ),
    ),
) -> Dict[str, Any]:
    """
    Sweeps RawScrapLaptop for pending entries and processes them through the
    Gemini LLM one at a time, with a mandatory delay between requests to
    respect the 5 RPM free-tier rate limit.

    - Default batch size: 20 (= daily quota). Pass ?limit=N to process fewer.
    - On a 429 / quota error: waits 65 s and retries once before marking failed.
    """
    pending_records = session.exec(
        select(RawScrapLaptop)
        .where(RawScrapLaptop.processing_status == "pending")
        .limit(limit)
    ).all()

    if not pending_records:
        return {"message": "No pending records found in the queue."}

    results_summary = []
    total_saved = 0
    total_updated = 0
    requests_made = 0

    for i, record in enumerate(pending_records):
        # Throttle: sleep before every request except the first
        if i > 0:
            logger.info(f"Rate-limit delay: sleeping {_INTER_REQUEST_DELAY_S}s before next request ({i+1}/{len(pending_records)})")
            time.sleep(_INTER_REQUEST_DELAY_S)

        logger.info(f"Processing [{i+1}/{len(pending_records)}]: {record.raw_product_name} (id={record.id})")
        res = process_raw_laptop_data(str(record.id), session)
        requests_made += 1

        # On rate-limit error: wait 65 s and retry once
        if res.get("status") == "error":
            error_msg = (res.get("message") or "").lower()
            is_rate_limited = any(kw in error_msg for kw in _RATE_LIMIT_KEYWORDS)

            if is_rate_limited:
                logger.warning(f"Rate limit hit on record {record.id}. Waiting {_RETRY_WAIT_S}s then retrying...")
                time.sleep(_RETRY_WAIT_S)
                res = process_raw_laptop_data(str(record.id), session)
                requests_made += 1

        results_summary.append({
            "raw_id": str(record.id),
            "product_name": record.raw_product_name,
            "status": res.get("status"),
            "variants_extracted": res.get("variants_extracted", 0),
            "variants_saved": res.get("variants_saved", 0),
            "variants_updated": res.get("variants_updated", 0),
            "error": res.get("message") if res.get("status") == "error" else None,
        })

        if res.get("status") == "success":
            total_saved   += res.get("variants_saved", 0)
            total_updated += res.get("variants_updated", 0)

    pending_remaining = session.exec(
        select(RawScrapLaptop).where(RawScrapLaptop.processing_status == "pending")
    ).all()

    return {
        "message": f"Bulk processing complete. {len(pending_records)} record(s) attempted.",
        "requests_made": requests_made,
        "total_new_variants_saved": total_saved,
        "total_variants_updated": total_updated,
        "pending_remaining": len(pending_remaining),
        "details": results_summary,
    }