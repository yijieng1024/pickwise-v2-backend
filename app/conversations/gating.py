"""
Module 4 — Relevance Gating
Even when Modules 2/3 return a non-empty candidate list, the top score might
still be too low to trust — meaning the catalog genuinely has nothing close
to what the user asked for.

This module intercepts those low-confidence results before they reach the user
and redirects the conversation toward clarification instead of surfacing a
misleading recommendation.

Key rule: gating ≠ ending the conversation.
A gated response always includes a constructive follow-up question that
identifies the most likely bottleneck and asks the user to loosen it.
"""
from dataclasses import dataclass, field
from typing import Literal, Optional

from app.conversations.reranker import RankedCandidate

# Placeholder — must be calibrated against Module 5's NDCG results once
# a labeled test set exists. 0.45 is the spec's suggested starting point.
RELEVANCE_THRESHOLD = 0.45


@dataclass
class GateResult:
    status: Literal["pass", "gated"]
    candidates: list[RankedCandidate] = field(default_factory=list)
    message: Optional[str] = None       # shown in chat when gated
    top_score: Optional[float] = None   # for diagnostics / Module 5 evaluation
    bottleneck: Optional[str] = None    # "budget" | "weight_limit" | "general"


def _detect_bottleneck(candidates: list[RankedCandidate]) -> str:
    """
    Scan penalty_reasons of the top candidates to find the most common culprit.
    Returns a string key used to select a targeted clarification question.
    """
    budget_hits = 0
    weight_hits = 0

    for c in candidates[:10]:  # only inspect the top slice
        for reason in c.penalty_reasons:
            if "budget" in reason or "over budget" in reason:
                budget_hits += 1
            if "weight" in reason:
                weight_hits += 1

    if budget_hits == 0 and weight_hits == 0:
        return "general"
    return "budget" if budget_hits >= weight_hits else "weight_limit"


def _build_gate_message(bottleneck: str, top_score: Optional[float]) -> str:
    messages = {
        "budget": (
            "I couldn't find a laptop that closely matches your needs within your current budget. "
            "Could you tell me how much flexibility you have on price? "
            "Even a small increase might open up much better options."
        ),
        "weight_limit": (
            "I couldn't find a lightweight laptop that also meets your other requirements — "
            "lightweight + high performance is a tough combination. "
            "Would you be open to a slightly heavier machine, or is portability the top priority?"
        ),
        "general": (
            "I couldn't find a laptop that closely matches your needs right now. "
            "Could you tell me which requirement you're most flexible on — "
            "budget, performance, portability, or brand?"
        ),
    }
    return messages.get(bottleneck, messages["general"])


def relevance_gate(candidates: list[RankedCandidate]) -> GateResult:
    """
    Check the top candidate's final_score against RELEVANCE_THRESHOLD.

    Pass  → return all candidates for PickScore + LLM explanation.
    Gated → block output, return a targeted clarification message instead.
    """
    if not candidates:
        return GateResult(
            status="gated",
            message=(
                "I couldn't find any laptops matching your search. "
                "Try broadening your requirements — for example, a higher budget "
                "or a different brand."
            ),
            top_score=None,
            bottleneck="general",
        )

    top_score = candidates[0].final_score

    if top_score < RELEVANCE_THRESHOLD:
        bottleneck = _detect_bottleneck(candidates)
        return GateResult(
            status="gated",
            message=_build_gate_message(bottleneck, top_score),
            top_score=top_score,
            bottleneck=bottleneck,
        )

    return GateResult(
        status="pass",
        candidates=candidates,
        top_score=top_score,
    )
