"""
Module 2 — Reranking Layer
Re-scores Module 1's candidate pool against the user's hard constraints.

Vector similarity is good at topic matching but is blind to explicit constraints
like "must be under RM 4000" or "must weigh less than 1.5kg". This module corrects
semantic drift by applying penalty multipliers and bonuses on top of the raw
similarity score.

Formula:
    final_score = similarity_score * penalty_multiplier + bonus
"""
from dataclasses import dataclass, field
from typing import Optional

from app.conversations.retrieval import RetrievalCandidate

# Weight threshold used when the user explicitly wants a portable laptop.
_PORTABILITY_WEIGHT_LIMIT_KG = 1.5

# Purpose keyword signals for the bonus: maps purpose → gpu/cpu keywords that
# indicate a strong match (checked against gpu_model / processor_model strings).
_PURPOSE_GPU_SIGNALS: dict[str, list[str]] = {
    "Gaming": ["rtx", "rx", "rog"],
    "Creative": ["rtx", "rx", "radeon"],
}
_PURPOSE_CPU_SIGNALS: dict[str, list[str]] = {
    "Programming": ["i5", "i7", "i9", "ryzen 5", "ryzen 7", "ryzen 9", "m3", "m4"],
    "Office": ["i5", "i7", "ryzen 5", "ryzen 7"],
}


@dataclass
class UserConstraints:
    """
    Decoupled from LaptopUserPreference so this module works with any
    constraint source — preference profile, dialogue state, or raw message.
    """
    budget: Optional[float] = None
    weight_limit: Optional[float] = None   # None = no weight constraint
    purpose: list[str] = field(default_factory=list)
    brand_preferences: list[str] = field(default_factory=list)

    @classmethod
    def from_preference(cls, pref) -> "UserConstraints":
        """Build from a LaptopUserPreference ORM object."""
        weight_limit = (
            _PORTABILITY_WEIGHT_LIMIT_KG if pref.portability == "Yes" else None
        )
        return cls(
            budget=float(pref.budget) if pref.budget else None,
            weight_limit=weight_limit,
            purpose=pref.purpose or [],
            brand_preferences=[b.lower() for b in (pref.brand_preferences or [])],
        )


@dataclass
class RankedCandidate:
    laptop_id: str
    product_name: str
    price_rm: float
    similarity_score: float
    penalty_multiplier: float
    bonus: float
    final_score: float
    penalty_reasons: list[str]
    bonus_reasons: list[str]

    # Keep the originals so downstream modules (PickScore, LLM) can access specs.
    _laptop: object = field(repr=False)
    _brand_name: str = field(repr=False)


def _budget_penalty(price: float, budget: Optional[float]) -> tuple[float, list[str]]:
    """
    Returns (multiplier, reasons).
    Within budget → 1.0 (no penalty).
    Soft overage (<10%) → decaying multiplier, never below 0.5.
    Hard overage (>30%) → 0.1 (near-elimination).
    """
    if budget is None or price <= budget:
        return 1.0, []

    over_ratio = (price - budget) / budget

    if over_ratio <= 0.10:
        multiplier = max(0.5, 1 - over_ratio * 2)
        return multiplier, [f"price RM {price:.0f} is {over_ratio*100:.0f}% over budget (soft penalty)"]

    if over_ratio <= 0.30:
        multiplier = max(0.3, 1 - over_ratio * 1.5)
        return multiplier, [f"price RM {price:.0f} is {over_ratio*100:.0f}% over budget (medium penalty)"]

    return 0.1, [f"price RM {price:.0f} is {over_ratio*100:.0f}% over budget (hard penalty)"]


def _weight_penalty(weight_kg: float, weight_limit: Optional[float]) -> tuple[float, list[str]]:
    """
    Only active when the user explicitly asked for a portable laptop.
    Over the limit by ≤20% → 0.7 multiplier.
    Over the limit by >20% → 0.2 multiplier (heavy gaming laptops penalised hard).
    """
    if weight_limit is None or weight_kg <= weight_limit:
        return 1.0, []

    over_ratio = (weight_kg - weight_limit) / weight_limit
    if over_ratio <= 0.20:
        return 0.7, [f"weight {weight_kg}kg exceeds {weight_limit}kg limit (soft penalty)"]
    return 0.2, [f"weight {weight_kg}kg is far above {weight_limit}kg limit (hard penalty)"]


def _purpose_bonus(laptop, purpose: list[str]) -> tuple[float, list[str]]:
    """
    Small additive bonus when the laptop's hardware signals match the purpose.
    Capped at +0.08 total so a bonus never outweighs a real penalty.
    """
    bonus = 0.0
    reasons: list[str] = []
    gpu = (laptop.gpu_model or "").lower()
    cpu = (laptop.processor_model or "").lower()

    for p in purpose:
        for kw in _PURPOSE_GPU_SIGNALS.get(p, []):
            if kw in gpu:
                bonus += 0.04
                reasons.append(f"GPU matches {p} purpose")
                break
        for kw in _PURPOSE_CPU_SIGNALS.get(p, []):
            if kw in cpu:
                bonus += 0.04
                reasons.append(f"CPU matches {p} purpose")
                break

    return min(bonus, 0.08), reasons


def _brand_bonus(brand_name: str, brand_preferences: list[str]) -> tuple[float, list[str]]:
    if not brand_preferences or "no preference" in brand_preferences:
        return 0.0, []
    if brand_name.lower() in brand_preferences:
        return 0.05, [f"brand {brand_name} matches preference"]
    # User explicitly named a brand — wrong brand is a real signal, not neutral
    return -0.25, [f"brand {brand_name} does not match preference"]


def rerank(
    candidates: list[RetrievalCandidate],
    constraints: UserConstraints,
) -> list[RankedCandidate]:
    """
    Score every candidate and return them sorted by final_score descending.
    Candidates with a hard budget penalty (multiplier=0.1) sink to the bottom
    naturally — no explicit removal, so Module 3 can still see them if needed.
    """
    ranked: list[RankedCandidate] = []

    for c in candidates:
        laptop = c.laptop
        penalty = 1.0
        penalty_reasons: list[str] = []

        b_mult, b_reasons = _budget_penalty(laptop.price_rm, constraints.budget)
        w_mult, w_reasons = _weight_penalty(laptop.weight_kg, constraints.weight_limit)

        penalty = b_mult * w_mult
        penalty_reasons = b_reasons + w_reasons

        p_bonus, p_reasons = _purpose_bonus(laptop, constraints.purpose)
        br_bonus, br_reasons = _brand_bonus(c.brand_name, constraints.brand_preferences)

        bonus = p_bonus + br_bonus
        bonus_reasons = p_reasons + br_reasons

        final_score = c.similarity_score * penalty + bonus

        ranked.append(RankedCandidate(
            laptop_id=str(laptop.id),
            product_name=f"{c.brand_name} {laptop.product_name}",
            price_rm=laptop.price_rm,
            similarity_score=c.similarity_score,
            penalty_multiplier=round(penalty, 3),
            bonus=round(bonus, 3),
            final_score=round(final_score, 4),
            penalty_reasons=penalty_reasons,
            bonus_reasons=bonus_reasons,
            _laptop=laptop,
            _brand_name=c.brand_name,
        ))

    ranked.sort(key=lambda x: x.final_score, reverse=True)
    return ranked
