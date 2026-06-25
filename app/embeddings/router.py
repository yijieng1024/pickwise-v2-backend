from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlmodel import Session

from app.database import get_session
from app.users.auth import get_current_admin
from app.laptops.laptop_models import Laptop, LaptopEmbedding
from app.embeddings.service import (
    generate_all_laptop_embeddings,
    generate_single_laptop_embedding,
)

router = APIRouter(prefix="/embeddings", tags=["Embeddings"])


@router.post("/laptops/generate-all", dependencies=[Depends(get_current_admin)])
def trigger_generate_all(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """
    Triggers embedding generation for every laptop in the catalog.

    WHY BackgroundTasks:
    Embedding the full catalog requires many Gemini API calls and can take
    minutes. BackgroundTasks lets FastAPI respond immediately (the work
    continues after the HTTP response is sent). Same pattern used by the
    benchmark scrapers in this codebase.

    Admin-only: external API calls cost money and could hit rate limits
    if triggered carelessly.
    """
    total = session.execute(select(func.count()).select_from(Laptop)).scalar()
    background_tasks.add_task(generate_all_laptop_embeddings, session)

    return {
        "message": "Embedding generation started in background",
        "total_laptops": total,
        "tip": "Poll GET /embeddings/laptops/status to track progress.",
    }


@router.post("/laptops/{laptop_id}", dependencies=[Depends(get_current_admin)])
def trigger_generate_single(
    laptop_id: UUID,
    session: Session = Depends(get_session),
):
    """
    Generates or refreshes the embedding for a single laptop immediately.

    WHY synchronous here (no BackgroundTasks):
    One API call takes ~1 second — fast enough to return the result directly.
    The caller gets immediate confirmation of success or failure, which is
    more useful than a deferred background task for a single item.
    """
    result = generate_single_laptop_embedding(session, laptop_id)
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.get("/laptops/status")
def get_embedding_status(session: Session = Depends(get_session)):
    """
    Shows how many laptops have been embedded vs. total.

    WHY this endpoint exists:
    Since generate-all runs in the background with no progress stream,
    you need a way to check completion. This gives you a live count of
    embedded vs. missing laptops so you know when the job is done.
    """
    total_laptops = session.execute(select(func.count()).select_from(Laptop)).scalar()
    total_embedded = session.execute(select(func.count()).select_from(LaptopEmbedding)).scalar()
    missing = total_laptops - total_embedded

    return {
        "total_laptops": total_laptops,
        "embedded": total_embedded,
        "missing": missing,
        "coverage_pct": round(total_embedded / total_laptops * 100, 1) if total_laptops > 0 else 0,
    }
