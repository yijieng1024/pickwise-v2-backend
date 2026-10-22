import uuid

from langchain_core.tools import tool
from sqlalchemy import or_
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.config import settings
from app.database import engine
from app.embeddings.service import embed_text
from app.taxonomy.category_model import Category  # noqa: F401 — must precede Laptop to register with SQLAlchemy mapper
from app.laptops.customization_model import LaptopCustomization  # must precede Laptop import
from app.laptops.laptop_models import Laptop
from app.reviews.models import ChunkScope, LaptopReviewChunk


@tool
def calculate_custom_apple_price(
    model_code: str,
    selected_options: list[str],
) -> dict:
    """
    Calculate the total price for an Apple laptop with selected customization options.

    Looks up the laptop by model_code, then sums the price_add_rm for each
    customization option whose option_name matches an entry in selected_options.

    Args:
        model_code: The laptop's unique model code (e.g. "MNEH3ZP/A").
        selected_options: Option names to add (e.g. ["32GB RAM", "1TB SSD"]).
    """
    with Session(engine) as session:
        laptop = session.exec(
            select(Laptop).where(Laptop.model_code == model_code)
        ).first()

        if not laptop:
            return {"error": f"Laptop with model_code '{model_code}' not found."}

        all_customizations = session.exec(
            select(LaptopCustomization).where(
                LaptopCustomization.laptop_id == laptop.id
            )
        ).all()

        selected_addons = [
            {
                "option_name": c.option_name,
                "category": c.category.name if c.category else None,
                "price_add_rm": c.price_add_rm,
            }
            for c in all_customizations
            if c.option_name in selected_options
        ]

        unrecognized = [
            opt for opt in selected_options
            if opt not in {c.option_name for c in all_customizations}
        ]

        total_addon_rm = sum(a["price_add_rm"] for a in selected_addons)

        return {
            "model_code": model_code,
            "product_name": laptop.product_name,
            "base_price_rm": laptop.price_rm,
            "selected_addons": selected_addons,
            "total_price_rm": laptop.price_rm + total_addon_rm,
            "unrecognized_options": unrecognized,
        }


_EVIDENCE_POOL = 20     # nearest rows fetched before the cutoff and dedup
_EVIDENCE_RESULTS = 3

_SCOPE_NOTE = (
    "NOTE: a line marked \"configuration not stated\" comes from a review that "
    "did not say which configuration was tested; a line marked \"tested on "
    "...\" was tested on another configuration of this product line. When you "
    "use one, tell the user so -- never present it as a test of this exact "
    "configuration."
)


def _applies_to(chunk: LaptopReviewChunk, laptop_id: uuid.UUID, names: dict) -> str:
    if chunk.laptop_id == laptop_id:
        return "this configuration"
    if chunk.laptop_id is None:
        return "this product line, configuration not stated"
    return f"this product line, tested on {names.get(chunk.laptop_id, 'another configuration')}"


@tool
def get_review_evidence(laptop_id: str, query_context: str) -> str:
    """
    Retrieve real reviewer opinions for a specific laptop, ranked by relevance to
    the user's stated priorities. Draws from ingested YouTube review transcripts.
    Each line says what it applies to: this exact configuration, or the
    product line with the tested configuration named or not stated.

    Args:
        laptop_id: UUID string of the laptop (from search_laptops results).
        query_context: The user's stated priorities or question (used for semantic matching).
    """
    # Two tiers, never mixed in one result (Review B2):
    #   1. this laptop's own chunks plus its family's family-scope chunks
    #   2. only when nothing in tier 1 is within the cutoff for THIS query:
    #      chunks its sibling configurations were tested on, each naming that
    #      config -- a sibling's frame rates are not this machine's, so they
    #      never sit beside its own evidence.
    # Other families are never used.
    try:
        target = uuid.UUID(laptop_id)
    except ValueError:
        return f"'{laptop_id}' is not a laptop id; pass the laptop_id from search_laptops."
    # Embedded before the session opens: no connection is held across the call.
    try:
        query_embedding = embed_text(query_context)
    except Exception as e:
        return f"Could not retrieve review evidence: {e}"

    distance_col = LaptopReviewChunk.embedding.cosine_distance(query_embedding)
    C = LaptopReviewChunk

    def nearest(session, where):
        return session.execute(
            sa_select(C, distance_col.label("distance"))
            .where(where)
            .order_by(distance_col.asc(), C.id)
            .limit(_EVIDENCE_POOL)
        ).all()

    def pick(rows):
        picked, seen = [], set()
        for chunk, dist in rows:
            if dist > settings.review_evidence_max_distance:
                break  # rows are nearest-first
            keys = {chunk.chunk_text.strip().casefold(), (chunk.video_id, chunk.timestamp_start_seconds)}
            if keys & seen:
                continue
            seen |= keys
            picked.append(chunk)
            if len(picked) == _EVIDENCE_RESULTS:
                break
        return picked

    with Session(engine) as session:
        laptop = session.get(Laptop, target)
        if laptop is None:
            return f"No laptop with id {laptop_id}."
        family_id = laptop.family_id
        own = C.laptop_id == target
        tier1 = own if family_id is None else or_(
            own, (C.family_id == family_id) & (C.scope == ChunkScope.FAMILY.value)
        )
        rows = nearest(session, tier1)
        picked = pick(rows)
        if not picked and family_id is not None:
            siblings = nearest(session, (C.family_id == family_id) & (C.laptop_id != target))
            rows = rows or siblings
            picked = pick(siblings)
        names = dict(session.exec(
            select(Laptop.id, Laptop.product_name).where(
                Laptop.id.in_({c.laptop_id for c in picked if c.laptop_id})  # type: ignore[attr-defined]
            )
        ).all()) if picked else {}

    if not rows:
        return "No review evidence found for this laptop yet."
    if not picked:
        return ("This laptop has review evidence, but none of it is relevant to: "
                f"{query_context}. Do not cite reviews for this point.")

    lines = []
    for chunk in picked:
        yt_link = (
            f"https://youtube.com/watch?v={chunk.video_id}"
            f"&t={chunk.timestamp_start_seconds}"
        )
        lines.append(
            f"[{chunk.sentiment_tag.upper()}] {chunk.chunk_text}\n"
            f"  Applies to: {_applies_to(chunk, target, names)}\n"
            f"  Source: {chunk.channel_name} — {yt_link}"
        )
    if any(c.laptop_id != target for c in picked):
        lines.append(_SCOPE_NOTE)
    return "\n\n".join(lines)
