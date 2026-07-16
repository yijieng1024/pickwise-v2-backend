import time
from datetime import datetime, timezone
from uuid import UUID

from langchain_openai import OpenAIEmbeddings
from sqlmodel import Session, select

from app.config import settings
from app.laptops.laptop_models import Laptop, LaptopEmbedding
from app.laptops.brand_model import LaptopBrand

# WHY llama-nemotron-embed with dimensions=768: served via OpenRouter's
# OpenAI-compatible /embeddings endpoint (replaced gemini-embedding-001).
# The model natively outputs 2048 dims but is Matryoshka-trained, so
# dimensions=768 keeps the existing Vector(768) columns without a schema
# migration. Vectors from this model live in a different embedding space
# than the old Gemini ones — after any model change, POST
# /embeddings/generate-all must be re-run or similarity scores are garbage.
# check_embedding_ctx_length=False is required on non-OpenAI backends:
# without it langchain-openai pre-tokenizes with tiktoken and sends
# token-ID arrays that OpenRouter cannot decode. encoding_format="float"
# is equally required: the OpenAI SDK defaults to requesting base64, which
# this provider answers with HTTP 200 and an empty data array
# ("No embedding data received").
_embedder = OpenAIEmbeddings(
    model="nvidia/llama-nemotron-embed-vl-1b-v2:free",
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
    dimensions=768,
    check_embedding_ctx_length=False,
    model_kwargs={"encoding_format": "float"},
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

    WHY the retry: OpenRouter's :free embeddings endpoint occasionally answers
    HTTP 200 with an empty data array (observed under bursty request rates),
    which surfaces from langchain-openai as TypeError/ValueError rather than a
    retryable API error. Brief backoff clears it; callers with their own
    fallback (rag/retrieval.py) still get an exception if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return _embedder.embed_query(text)
        except (TypeError, ValueError) as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Embedding call returned no data after 3 attempts: {last_exc}") from last_exc


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


def generate_all_laptop_embeddings(session: Session) -> dict:
    """
    WHY this runs as a background task (called from router via BackgroundTasks):
    Each embedding requires one API call to Gemini. With 50 laptops that is
    50 network round-trips — could take 30–60 seconds. We never block the HTTP
    response for that long. FastAPI BackgroundTasks lets the API return
    immediately while this continues running after the response is sent.

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

    succeeded, failed = 0, 0
    errors = []

    for laptop in laptops:
        try:
            brand_name = brand_map.get(laptop.brand_id, "Unknown")
            text = build_laptop_embedding_text(laptop, brand_name)
            vector = embed_text(text)
            upsert_laptop_embedding(session, laptop.id, vector)
            succeeded += 1
            time.sleep(3.5)  # OpenRouter :free models allow ~20 requests/min
        except Exception as e:
            failed += 1
            errors.append({"laptop_id": str(laptop.id), "error": str(e)})

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
