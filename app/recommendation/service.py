from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.config import settings
from app.embeddings.service import embed_text
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopEmbedding
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.pickscore.engine import calculate_pick_score
from app.recommendation.schemas import (
    RecommendationResponse,
    RecommendedLaptop,
    _LLMOutput,
)
from app.users.models import LaptopUserPreference

_TECH_SAVVY_INSTRUCTION = {
    "Very tech-savvy": (
        "Use technical terminology freely — benchmark scores, TDP, clock speeds, VRAM, "
        "storage IOPS. The user is comfortable with specs."
    ),
    "Somewhat tech-savvy": (
        "Balance technical terms with brief plain-English context. Mention specs but "
        "also explain what they mean in practice."
    ),
    "Not very tech-savvy": (
        "Use plain, everyday language. Focus entirely on real-world outcomes "
        "(e.g. 'handles video editing smoothly', 'lasts a full workday'). Avoid jargon."
    ),
}

_SYSTEM_PROMPT = (
    "You are PickWise, an expert laptop recommendation assistant. "
    "Your job is to explain why each shortlisted laptop does or does not suit the user, "
    "then recommend the best overall pick.\n"
    "Communication style: {tech_instruction}"
)

_HUMAN_PROMPT = (
    'User query: "{query}"\n\n'
    "User profile:\n{pref_context}\n\n"
    "Shortlisted laptops (ranked by PickScore, highest first):\n\n{laptops_block}\n\n"
    "Instructions:\n"
    "- For each laptop write a 2-3 sentence explanation of how well it fits the user's needs.\n"
    "- Then write a 3-4 sentence overall summary recommending the single best pick and why.\n"
    "- Use the exact laptop_id strings shown above (UUIDs) in your explanations list.\n"
    "- Match your language to the communication style specified above."
)


def _build_laptop_block(laptop: Laptop, brand_name: str, pick_resp, similarity: float) -> str:
    breakdown_str = " | ".join(
        f"{f.factor}={f.raw_score:.0f}" for f in pick_resp.breakdown
    )
    return (
        f"laptop_id: {laptop.id}\n"
        f"Name: {brand_name} {laptop.product_name}\n"
        f"Price: RM {laptop.price_rm:.0f}\n"
        f"PickScore: {pick_resp.score}/100  (mode: {pick_resp.mode})\n"
        f"CPU: {laptop.processor_model} | GPU: {laptop.gpu_model}\n"
        f"RAM: {laptop.ram_gb}GB | Storage: {laptop.ssd_gb}GB {laptop.storage_type or ''}\n"
        f"Display: {laptop.display_size_inch}\" | Weight: {laptop.weight_kg}kg | Battery: {laptop.battery_wh}Wh\n"
        f"Score breakdown: {breakdown_str}\n"
        f"Semantic similarity: {similarity:.3f}"
    )


def get_recommendations(
    query: str,
    user_id: UUID,
    session: Session,
    budget_max: Optional[float],
    brand: Optional[str],
    top_k: int,
    candidate_pool_size: int,
) -> RecommendationResponse:
    # 1. Require preference profile
    user_pref = session.exec(
        select(LaptopUserPreference).where(LaptopUserPreference.user_id == user_id)
    ).first()
    if not user_pref:
        raise HTTPException(
            status_code=400,
            detail="Please complete your preference profile before requesting recommendations.",
        )

    # 2. Hybrid vector search — retrieve candidate pool
    query_vector = embed_text(query)
    distance_col = LaptopEmbedding.embedding.cosine_distance(query_vector)
    stmt = (
        sa_select(Laptop, LaptopBrand.name, distance_col.label("distance")) # type: ignore
        .join(LaptopEmbedding, LaptopEmbedding.laptop_id == Laptop.id)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
    )
    if budget_max is not None:
        stmt = stmt.where(Laptop.price_rm <= budget_max)
    if brand is not None:
        stmt = stmt.where(LaptopBrand.name.ilike(brand))
    stmt = stmt.order_by(distance_col.asc()).limit(candidate_pool_size)
    rows = session.execute(stmt).all()

    if not rows:
        raise HTTPException(status_code=404, detail="No laptops found matching your query.")

    # 3. Batch PickScore — fetch shared data once, score every candidate
    ranges = get_laptop_ranges(session)
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]

    scored: list[tuple[Laptop, str, float, object]] = []
    for laptop, brand_name, row_distance in rows:
        product = laptop_to_scorable(laptop, brand_name)
        pick_resp = calculate_pick_score(product, user_pref, ranges, cpu_benchmarks, gpu_benchmarks)
        scored.append((laptop, brand_name, round(1 - row_distance, 4), pick_resp))

    # 4. Re-rank by PickScore, keep top_k
    scored.sort(key=lambda x: x[3].score, reverse=True)  # type: ignore[attr-defined]
    top = scored[:top_k]

    # 5. Build LLM prompt context
    tech_instruction = _TECH_SAVVY_INSTRUCTION.get(
        user_pref.tech_savviness or "",
        _TECH_SAVVY_INSTRUCTION["Somewhat tech-savvy"],
    )
    pref_context = (
        f"Budget: RM {user_pref.budget or 'not set'} | "
        f"Purpose: {', '.join(user_pref.purpose or ['not specified'])} | "
        f"Portability preference: {user_pref.portability or 'neutral'} | "
        f"Brand preferences: {', '.join(user_pref.brand_preferences or ['none'])}"
    )
    laptops_block = "\n\n".join(
        _build_laptop_block(laptop, brand_name, pick_resp, sim)
        for laptop, brand_name, sim, pick_resp in top
    )

    # 6. Call Gemini with structured output
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash",
        temperature=0.3,
        google_api_key=settings.gemini_api_key,
    )
    structured_llm = llm.with_structured_output(_LLMOutput)
    chain = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        ("human", _HUMAN_PROMPT),
    ]) | structured_llm

    llm_output: _LLMOutput = chain.invoke({  # type: ignore[assignment]
        "tech_instruction": tech_instruction,
        "query": query,
        "pref_context": pref_context,
        "laptops_block": laptops_block,
    })

    # 7. Map explanations by laptop_id and assemble response
    explanation_map = {e.laptop_id: e.explanation for e in llm_output.explanations}

    recommendations = [
        RecommendedLaptop(
            laptop_id=laptop.id,
            product_name=f"{brand_name} {laptop.product_name}",
            price_rm=laptop.price_rm,
            pick_score=pick_resp.score,  # type: ignore[attr-defined]
            similarity_score=sim,
            breakdown=pick_resp.breakdown,  # type: ignore[attr-defined]
            explanation=explanation_map.get(str(laptop.id), ""),
        )
        for laptop, brand_name, sim, pick_resp in top
    ]

    return RecommendationResponse(
        query=query,
        recommendations=recommendations,
        summary=llm_output.summary,
    )
