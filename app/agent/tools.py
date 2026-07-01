from typing import Optional

from langchain_core.tools import tool
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.database import engine
from app.embeddings.service import embed_text
from app.laptops.customization_model import LaptopCustomization  # must precede Laptop import
from app.laptops.laptop_models import Laptop
from app.laptops.brand_model import LaptopBrand
from app.reviews.models import LaptopReviewChunk


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
                "category": c.category,
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


@tool
def search_laptops(
    budget_max: Optional[float] = None,
    brand: Optional[str] = None,
    min_ram_gb: Optional[int] = None,
) -> str:
    """
    Search the PickWise laptop catalog by budget, brand, or minimum RAM.
    Returns up to 5 matching laptops with key specs and prices in RM.

    Args:
        budget_max: Maximum price in RM (e.g. 5000.0).
        brand: Brand name, case-insensitive partial match (e.g. "apple", "asus").
        min_ram_gb: Minimum RAM in GB (e.g. 16).
    """
    with Session(engine) as session:
        stmt = (
            sa_select(Laptop, LaptopBrand.name)
            .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        )
        if budget_max is not None:
            stmt = stmt.where(Laptop.price_rm <= budget_max)
        if brand is not None:
            stmt = stmt.where(LaptopBrand.name.ilike(f"%{brand}%"))
        if min_ram_gb is not None:
            stmt = stmt.where(Laptop.ram_gb >= min_ram_gb)

        stmt = stmt.order_by(Laptop.price_rm.asc()).limit(5)
        rows = session.execute(stmt).all()

    if not rows:
        return "No laptops found matching those criteria."

    lines = []
    for laptop, brand_name in rows:
        lines.append(
            f"• {brand_name} {laptop.product_name} | RM {laptop.price_rm:.0f} | "
            f"CPU: {laptop.processor_model} | GPU: {laptop.gpu_model} | "
            f"RAM: {laptop.ram_gb}GB | Storage: {laptop.ssd_gb}GB | "
            f"Display: {laptop.display_size_inch}\" | Weight: {laptop.weight_kg}kg | "
            f"Model code: {laptop.model_code}"
        )
    return "\n".join(lines)


@tool
def get_review_evidence(laptop_id: str, query_context: str) -> str:
    """
    Retrieve real reviewer opinions for a specific laptop, ranked by relevance to the
    user's stated priorities. Draws from ingested YouTube review transcripts.

    Args:
        laptop_id: UUID string of the laptop (from search_laptops results).
        query_context: The user's stated priorities or question (used for semantic matching).
    """
    try:
        query_embedding = embed_text(query_context)
    except Exception as e:
        return f"Could not retrieve review evidence: {e}"

    distance_col = LaptopReviewChunk.embedding.cosine_distance(query_embedding)

    with Session(engine) as session:
        rows = session.execute(
            sa_select(LaptopReviewChunk, distance_col.label("distance"))
            .where(LaptopReviewChunk.laptop_id == laptop_id)
            .order_by(distance_col.asc())
            .limit(3)
        ).all()

    if not rows:
        return "No review evidence found for this laptop yet."

    lines = []
    for chunk, _dist in rows:
        yt_link = (
            f"https://youtube.com/watch?v={chunk.video_id}"
            f"&t={chunk.timestamp_start_seconds}"
        )
        lines.append(
            f"[{chunk.sentiment_tag.upper()}] {chunk.chunk_text}\n"
            f"  Source: {chunk.channel_name} — {yt_link}"
        )
    return "\n\n".join(lines)
