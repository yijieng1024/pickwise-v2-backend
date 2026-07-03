"""
Module 3 — Constraint Relaxation
Triggered when Module 2 returns zero viable candidates.

Instead of replying "no results found", the system automatically loosens one
constraint dimension at a time, re-runs retrieval + reranking, and tells the
user exactly what was relaxed and why — so the conversation stays productive.

Priority order (least-sensitive → most-sensitive):
  1. Weight limit  (secondary spec — users are usually flexible)
  2. Budget        (relax modestly, +RM 500 per step, up to 3 steps)
  3. Brand         → NEVER auto-relaxed; must explicitly ask the user
"""
from copy import copy
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session

from app.rag.reranker import RankedCandidate, UserConstraints, rerank
from app.rag.retrieval import retrieve_candidates

# A candidate is "viable" if its final_score clears this floor.
# Below this, penalties are so heavy the result would be misleading to show.
_MIN_VIABLE_SCORE = 0.25

_RELAXATION_STEPS = [
    {"field": "weight_limit", "step": 0.2, "max_steps": 2},
    {"field": "budget",       "step": 500, "max_steps": 3},
]


@dataclass
class RelaxationResult:
    candidates: list[RankedCandidate]
    relaxed_field: str          # "budget" or "weight_limit"
    original_value: float
    relaxed_value: float
    user_message: str           # shown directly in the chat response


def _viable(candidates: list[RankedCandidate]) -> list[RankedCandidate]:
    return [c for c in candidates if c.final_score >= _MIN_VIABLE_SCORE]


def _build_message(field: str, original: float, relaxed: float) -> str:
    if field == "budget":
        return (
            f"I couldn't find a great match within RM {original:.0f}, "
            f"so I've slightly expanded the budget to RM {relaxed:.0f}. "
            f"Here's what I found:"
        )
    if field == "weight_limit":
        return (
            f"I couldn't find a laptop under {original:.1f}kg that fits your needs, "
            f"so I've relaxed the weight limit to {relaxed:.1f}kg. "
            f"Here's what I found:"
        )
    return f"I've relaxed {field} from {original} to {relaxed} to find results."


def needs_relaxation(candidates: list[RankedCandidate]) -> bool:
    """Return True when there are no viable candidates after reranking."""
    return len(_viable(candidates)) == 0


def relax_and_retry(
    query: str,
    constraints: UserConstraints,
    session: Session,
    step_index: int = 0,
) -> Optional[RelaxationResult]:
    """
    Recursively relax one constraint dimension per call.
    Returns None when all relaxation strategies are exhausted
    (Module 4 — Relevance Gating takes over from here).
    """
    if step_index >= len(_RELAXATION_STEPS):
        return None

    rule = _RELAXATION_STEPS[step_index]
    field = rule["field"]
    step = rule["step"]
    max_steps = rule["max_steps"]

    original_value = getattr(constraints, field)

    # Skip this rule if the constraint isn't active
    if original_value is None:
        return relax_and_retry(query, constraints, session, step_index + 1)

    for n in range(1, max_steps + 1):
        relaxed_constraints = copy(constraints)
        relaxed_value = original_value + step * n
        setattr(relaxed_constraints, field, relaxed_value)

        # Re-run the full retrieve → rerank pipeline with relaxed constraints
        candidates = retrieve_candidates(
            query=query,
            session=session,
            budget_max=relaxed_constraints.budget,
        )
        ranked = rerank(candidates, relaxed_constraints)

        if _viable(ranked):
            return RelaxationResult(
                candidates=ranked,
                relaxed_field=field,
                original_value=original_value,
                relaxed_value=relaxed_value,
                user_message=_build_message(field, original_value, relaxed_value),
            )

    # This dimension is exhausted — try the next one
    return relax_and_retry(query, constraints, session, step_index + 1)
