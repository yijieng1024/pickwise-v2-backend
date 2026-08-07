import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from sqlmodel import Session, select

if TYPE_CHECKING:
    # Type-only: importing job_service at runtime would make this module depend
    # on the job machinery it is merely reporting to.
    from app.common.job_service import JobProgress

from app.config import settings
from app.laptops.laptop_models import Laptop, LaptopEmbedding
from app.laptops.brand_model import LaptopBrand

# WHY gemini-embedding-2 with output_dimensionality=768 (replaced the
# OpenRouter nvidia/llama-nemotron-embed detour): the model defaults to a
# larger dimensionality, so output_dimensionality pins it to 768 to match
# the existing Vector(768) columns without a schema migration. Vectors from
# this model live in a different embedding space than any previous model —
# after any model change, POST /embeddings/generate-all must be re-run (and
# review chunks re-processed) or similarity scores are garbage.
_embedder = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-2",
    google_api_key=settings.gemini_api_key,
    output_dimensionality=768,
)


def build_laptop_embedding_text(laptop: Laptop, brand_name: str) -> str:
    """
    WHY we build a natural language document instead of embedding raw JSON:
    Embedding models are trained on natural language. A sentence like
    "ASUS ROG 16-inch gaming laptop 16GB RAM RTX 4060 RM 5999" activates
    the model's understanding of gaming, performance, and price far better
    than a raw dict like {"ram_gb": 16, "price_rm": 5999}.

    The richer and more descriptive the text, the better the semantic search.
    Future product types (phones, tablets) will have their own text builders.
    """
    lines = [
        f"{brand_name} {laptop.product_name}",
        f"Price: RM {laptop.price_rm:.0f}",
        f"Processor: {laptop.processor_model}",
        f"GPU: {laptop.gpu_model}",
        f"RAM: {laptop.ram_gb}GB{' ' + laptop.ram_type if laptop.ram_type else ''}",
        f"Storage: {laptop.ssd_gb}GB {laptop.storage_type or 'SSD'}",
        f"Display: {laptop.display_size_inch}-inch"
        + (f" {laptop.display_resolution}" if laptop.display_resolution else "")
        + (f" {laptop.display_type}" if laptop.display_type else "")
        + (f" {laptop.display_refresh_rate_hz}Hz" if laptop.display_refresh_rate_hz else ""),
        f"Weight: {laptop.weight_kg}kg",
        f"Battery: {laptop.battery_wh}Wh",
    ]

    if laptop.os:
        lines.append(f"OS: {laptop.os}")
    if laptop.ai_ready:
        lines.append("AI Ready: Yes | NPU on-chip")
    if laptop.npu_model:
        lines.append(f"NPU: {laptop.npu_model}" + (f" {laptop.npu_tops} TOPS" if laptop.npu_tops else ""))
    if laptop.release_year:
        lines.append(f"Released: {laptop.release_year}")
    if laptop.touchscreen:
        lines.append("Touchscreen: Yes")
    if laptop.fingerprint_reader:
        lines.append("Fingerprint reader: Yes")

    return " | ".join(lines)


def embed_text(text: str) -> list[float]:
    """
    WHY a thin wrapper:
    Centralises the embedding API call so every caller (laptop, future phone)
    uses the same model and can be swapped in one place if the model changes.
    """
    return _embedder.embed_query(text)


def upsert_laptop_embedding(session: Session, laptop_id: UUID, vector: list[float]) -> None:
    """
    WHY upsert instead of insert:
    Makes the operation idempotent — safe to run multiple times without
    creating duplicates. If the laptop already has an embedding we refresh it;
    if not, we create it. Same result regardless of how many times you call it.
    """
    existing = session.exec(
        select(LaptopEmbedding).where(LaptopEmbedding.laptop_id == laptop_id)
    ).first()

    if existing:
        existing.embedding = vector
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
    else:
        session.add(LaptopEmbedding(laptop_id=laptop_id, embedding=vector))

    session.commit()


def generate_all_laptop_embeddings(
    session: Session, progress: Optional["JobProgress"] = None
) -> dict:
    """
    WHY this runs as a background task (driven by job_service.run_job):
    Each embedding requires one API call to Gemini. With 277 laptops that is
    277 network round-trips — minutes of work. We never block the HTTP response
    for that long; the router returns 202 with a job to poll.

    *progress* is optional so this stays callable directly (tests, a shell, a
    future CLI). When supplied, each laptop reports through it, which is what
    turns `GET /jobs/{id}` into a live progress feed instead of the caller
    having to watch the embedded count climb.

    WHY we pre-fetch all brands in one query:
    Without this, each laptop triggers a separate DB query for its brand name
    — that is N+1 queries for N laptops. One upfront query for all brands is
    far more efficient.

    WHY we sleep between API calls:
    Gemini's embedding API has a rate limit (requests per minute). A short
    pause prevents hitting that limit on larger catalogs.
    """
    laptops = session.exec(select(Laptop)).all()

    brands = session.exec(select(LaptopBrand)).all()
    brand_map = {b.id: b.name for b in brands}

    # The router estimated from a count taken before the job started; correct it
    # to what this run will actually touch.
    if progress is not None:
        progress.set_total(len(laptops))

    succeeded, failed = 0, 0
    errors = []

    for laptop in laptops:
        try:
            brand_name = brand_map.get(laptop.brand_id, "Unknown")
            text = build_laptop_embedding_text(laptop, brand_name)
            vector = embed_text(text)
            upsert_laptop_embedding(session, laptop.id, vector)
            succeeded += 1
            if progress is not None:
                progress.advance(succeeded=True)
            time.sleep(0.3)  # respect Gemini rate limits
        except Exception as e:
            failed += 1
            errors.append({"laptop_id": str(laptop.id), "error": str(e)})
            if progress is not None:
                # `item` is what the jobs UI shows beside the error, so name the
                # laptop rather than its uuid.
                progress.advance(
                    succeeded=False,
                    item=f"{brand_map.get(laptop.brand_id, '')} {laptop.product_name}".strip(),
                    error=str(e),
                )

    return {
        "total": len(laptops),
        "succeeded": succeeded,
        "failed": failed,
        "errors": errors,
    }


def generate_single_laptop_embedding(session: Session, laptop_id: UUID) -> dict:
    """
    WHY a single-item version:
    When the AI processor adds a new laptop, we immediately embed it without
    re-running the entire catalog — keeps the search index fresh in real time.
    """
    laptop = session.exec(select(Laptop).where(Laptop.id == laptop_id)).first()
    if not laptop:
        return {"status": "error", "message": "Laptop not found"}

    brand = session.exec(select(LaptopBrand).where(LaptopBrand.id == laptop.brand_id)).first()
    brand_name = brand.name if brand else "Unknown"

    try:
        text = build_laptop_embedding_text(laptop, brand_name)
        vector = embed_text(text)
        upsert_laptop_embedding(session, laptop.id, vector)
        return {"status": "success", "laptop_id": str(laptop_id), "text_preview": text[:120]}
    except Exception as e:
        return {"status": "error", "message": str(e)}
