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
from app.logger import get_logger
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopEmbedding, LaptopStatus

logger = get_logger(__name__)

# Wide net — retrieve many candidates so the reranker has room to work.
# Precision is NOT the goal here; recall is.
_DEFAULT_RECALL_SIZE = 50

# In-process query cache: maps query text → (vector, timestamp)
# Avoids redundant Gemini API calls for repeated queries within the same session.
_QUERY_CACHE: dict[str, tuple[list[float], float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes

# The placeholder similarity every _relational_fallback row carries.
#
# Derived, not picked. It has to satisfy two constraints at once:
#
#   - above gating.RELEVANCE_THRESHOLD (0.53), or every fallback result is
#     gated and the rescue path rescues nothing -- which is what 0.5 did;
#   - below the bottom of the real score distribution, so a placeholder never
#     outranks a genuine semantic match.
#
# Measured over the 241 rows in pipeline_eval_logs (2026-09-14): p10 0.5867,
# p25 0.6270, p50 0.6778, p90 0.7754. 0.58 sits 0.05 above the gate and just
# under p10, so a fallback row reaches the user while sorting beneath roughly
# nine out of ten genuine hits.
#
# Re-derive alongside RELEVANCE_THRESHOLD on any embedding-model change: both
# are properties of the same score distribution, and a model swap moves it.
_FALLBACK_SIMILARITY = 0.58


@dataclass
class RetrievalCandidate:
    laptop: Laptop
    brand_name: str
    cosine_distance: float
    # Which retrieval path produced this row. The fallback's score is a
    # placeholder, not a measurement, so anything reading the score needs to
    # be able to tell the two apart -- and it must not be inferable from the
    # score itself, which is a constant that can and does move.
    from_fallback: bool = False

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
        # ERROR, not warning: semantic search is the product, and what replaces
        # it is a price-ordered SQL list that answers a different question.
        # exc_info keeps the traceback — the cause (quota, timeout, bad key)
        # decides whether this is a five-minute blip or an outage, and a
        # formatted message would throw that away.
        logger.error(
            "Embedding call failed for query %r — falling back to relational retrieval",
            query,
            exc_info=True,
        )
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

    # Laptop.id closes the order: configurations of one machine embed to very
    # similar (sometimes identical) vectors, so cosine distance ties, and an
    # unbroken tie makes the recall window's contents vary between identical
    # queries.
    stmt = stmt.order_by(distance_col.asc(), Laptop.id).limit(recall_size)
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
    Returns laptops ordered by price (ascending) with a fixed placeholder
    similarity (_FALLBACK_SIMILARITY) so downstream modules can still run.
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
    stmt = stmt.order_by(Laptop.price_rm.asc(), Laptop.id).limit(limit)  # type: ignore

    rows = session.execute(stmt).all()
    # similarity_score is 1 - cosine_distance, so the distance is the inverse of
    # the placeholder. Downstream reads from_fallback, never the score -- which
    # is exactly the mistake this constant's history records.
    return [
        RetrievalCandidate(
            laptop=laptop,
            brand_name=brand_name,
            cosine_distance=1.0 - _FALLBACK_SIMILARITY,
            from_fallback=True,
        )
        for laptop, brand_name in rows
    ]
