"""
Module 1 — Online Retrieval
Converts a user's natural-language query into a vector in real time and
returns a wide candidate pool from pgvector for the reranker to narrow down.
"""
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select as sa_select
from sqlmodel import Session

from app.embeddings.service import embed_text
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopEmbedding, LaptopStatus

# Wide net — retrieve many candidates so the reranker has room to work.
# Precision is NOT the goal here; recall is.
_DEFAULT_RECALL_SIZE = 50

# In-process query cache: maps query text → (vector, timestamp)
# Avoids redundant Gemini API calls for repeated queries within the same session.
_QUERY_CACHE: dict[str, tuple[list[float], float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class RetrievalCandidate:
    laptop: Laptop
    brand_name: str
    cosine_distance: float

    @property
    def similarity_score(self) -> float:
        return round(1 - self.cosine_distance, 4)


def _get_query_vector(query: str) -> list[float]:
    """Return cached vector if fresh, otherwise embed and cache."""
    now = time.monotonic()
    cached = _QUERY_CACHE.get(query)
    if cached and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    vector = embed_text(query)
    _QUERY_CACHE[query] = (vector, now)
    return vector


def retrieve_candidates(
    query: str,
    session: Session,
    budget_max: Optional[float] = None,
    brand: Optional[str] = None,
    recall_size: int = _DEFAULT_RECALL_SIZE,
) -> list[RetrievalCandidate]:
    """
    Embed the query and run pgvector cosine similarity search.

    Hard filters (budget_max, brand) are applied at the SQL level — they are
    absolute constraints that the reranker must never override. So is the
    active-status filter: an inactive/suspended laptop is never recommendable,
    and filtering here (rather than in the tool) covers the relaxation retries
    and the offline pipeline eval, which re-enter through this same function.

    Falls back to an unfiltered relational query (all laptops ordered by price)
    if the Gemini embedding call times out or fails, so the conversation never
    stalls completely.
    """
    try:
        query_vector = _get_query_vector(query)
    except Exception:
        return _relational_fallback(session, budget_max, brand, recall_size)

    distance_col = LaptopEmbedding.embedding.cosine_distance(query_vector)

    stmt = (
        sa_select(Laptop, LaptopBrand.name, distance_col.label("distance"))  # type: ignore
        .join(LaptopEmbedding, LaptopEmbedding.laptop_id == Laptop.id)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .where(Laptop.status == LaptopStatus.ACTIVE.value)
    )

    if budget_max is not None:
        stmt = stmt.where(Laptop.price_rm <= budget_max)
    if brand is not None:
        stmt = stmt.where(LaptopBrand.name.ilike(brand))

    stmt = stmt.order_by(distance_col.asc()).limit(recall_size)
    rows = session.execute(stmt).all()

    return [
        RetrievalCandidate(laptop=laptop, brand_name=brand_name, cosine_distance=distance)
        for laptop, brand_name, distance in rows
    ]


def _relational_fallback(
    session: Session,
    budget_max: Optional[float],
    brand: Optional[str],
    limit: int,
) -> list[RetrievalCandidate]:
    """
    Pure SQL fallback when the embedding API is unavailable.
    Returns laptops ordered by price (ascending) with similarity_score = 0.5
    as a neutral placeholder so downstream modules can still run.
    """
    from sqlmodel import select

    stmt = (
        select(Laptop, LaptopBrand.name)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .where(Laptop.status == LaptopStatus.ACTIVE.value)
    )
    if budget_max is not None:
        stmt = stmt.where(Laptop.price_rm <= budget_max)
    if brand is not None:
        stmt = stmt.where(LaptopBrand.name.ilike(brand))
    stmt = stmt.order_by(Laptop.price_rm.asc()).limit(limit)  # type: ignore

    rows = session.execute(stmt).all()
    # cosine_distance=0.5 → similarity_score=0.5 (neutral, not misleading)
    return [
        RetrievalCandidate(laptop=laptop, brand_name=brand_name, cosine_distance=0.5)
        for laptop, brand_name in rows
    ]
