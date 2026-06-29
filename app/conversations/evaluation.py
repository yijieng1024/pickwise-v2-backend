"""
Module 5 — NDCG Evaluation
Offline evaluation utility that measures recommendation quality across the
full retrieve → rerank → relax → gate pipeline.

This module is NOT called during live conversations. It is run:
  - Manually during development to calibrate Module 4's threshold
  - In CI after any change to retrieval, reranking, or relaxation logic
  - Whenever new laptops are added to the catalog (catalog shift can change scores)

Relevance scale (must be hand-labelled per test case):
  3 — fully matches the user's need (right purpose, within budget, right weight)
  1 — partially matches (right purpose, slightly over budget OR slightly too heavy)
  0 — irrelevant (wrong purpose, massively over budget, etc.)
"""
from dataclasses import dataclass, field
from typing import Literal, Optional

import json
import uuid

import numpy as np
from sqlmodel import Session

from app.conversations.gating import GateResult, relevance_gate
from app.conversations.relaxation import needs_relaxation, relax_and_retry
from app.conversations.reranker import UserConstraints, rerank
from app.conversations.retrieval import retrieve_candidates
from app.logger import get_eval_logger

# Structured JSON-lines file — one entry per pipeline run, easy to grep in CI
_eval_file_logger = get_eval_logger()


# ── Core NDCG algorithm (from spec) ──────────────────────────────────────────

def dcg_at_k(relevance_scores: list[int], k: int) -> float:
    """
    Discounted Cumulative Gain at K.
    Rewards relevant results that appear early in the ranking — a score of 3
    at position 1 contributes more than a score of 3 at position 5.
    """
    scores = np.array(relevance_scores[:k], dtype=float)
    # positions: 1-indexed → log2(pos+1); np.arange(2, k+2) gives [2,3,...,k+1]
    discounts = np.log2(np.arange(2, len(scores) + 2))
    return float(np.sum(scores / discounts))


def ndcg_at_k(
    relevance_scores: list[int],
    ideal_scores: list[int],
    k: int = 10,
) -> float:
    """
    Normalised DCG: DCG / IDCG (ideal DCG).
    Output is always in [0, 1]. 1.0 means the system ranked results in
    perfect order; 0.0 means all relevant results were ranked last.
    """
    dcg = dcg_at_k(relevance_scores, k)
    idcg = dcg_at_k(sorted(ideal_scores, reverse=True), k)
    return dcg / idcg if idcg > 0 else 0.0


# ── Test case structure ───────────────────────────────────────────────────────

@dataclass
class TestCase:
    """
    One labelled evaluation example.

    ground_truth maps laptop_id (string) → relevance score (3 / 1 / 0).
    Any laptop_id returned by the pipeline but not in ground_truth is
    treated as relevance 0 (irrelevant).
    """
    query: str
    constraints: UserConstraints
    ground_truth: dict[str, int]
    scenario_type: Literal["normal", "relaxation", "gating"] = "normal"
    description: str = ""


@dataclass
class CaseResult:
    query: str
    scenario_type: str
    ndcg_at_10: float
    gate_status: str                    # "pass" | "gated"
    relaxed_field: Optional[str]        # None if no relaxation occurred
    system_ranking: list[str]           # ordered laptop_ids from pipeline
    system_relevance: list[int]         # mapped relevance scores


@dataclass
class EvaluationReport:
    avg_ndcg_at_10: float
    per_case: list[CaseResult]
    by_scenario: dict[str, float]       # avg NDCG per scenario type


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _run_pipeline(
    query: str,
    constraints: UserConstraints,
    session: Session,
) -> tuple[GateResult, Optional[str]]:
    """
    Run the full CRS pipeline for one query.
    Returns (GateResult, relaxed_field_or_None).
    """
    candidates = retrieve_candidates(
        query=query,
        session=session,
        budget_max=constraints.budget,
    )
    ranked = rerank(candidates, constraints)

    relaxed_field = None

    if needs_relaxation(ranked):
        relaxation = relax_and_retry(query, constraints, session)
        if relaxation:
            ranked = relaxation.candidates
            relaxed_field = relaxation.relaxed_field
        else:
            ranked = []

    gate = relevance_gate(ranked)
    return gate, relaxed_field


# ── Evaluation runner ─────────────────────────────────────────────────────────

def evaluate_test_suite(
    test_cases: list[TestCase],
    session: Session,
    k: int = 10,
) -> EvaluationReport:
    """
    Run every test case through the pipeline, compute NDCG@k for each,
    and return a full report.
    """
    results: list[CaseResult] = []

    for case in test_cases:
        gate, relaxed_field = _run_pipeline(case.query, case.constraints, session)

        if gate.status == "gated" or not gate.candidates:
            # Gated = no ranking output → NDCG is 0 for this case
            results.append(CaseResult(
                query=case.query,
                scenario_type=case.scenario_type,
                ndcg_at_10=0.0,
                gate_status="gated",
                relaxed_field=relaxed_field,
                system_ranking=[],
                system_relevance=[],
            ))
            continue

        system_ranking = [c.laptop_id for c in gate.candidates[:k]]
        system_relevance = [case.ground_truth.get(lid, 0) for lid in system_ranking]
        ideal_relevance = sorted(case.ground_truth.values(), reverse=True)

        score = ndcg_at_k(system_relevance, ideal_relevance, k=k)

        results.append(CaseResult(
            query=case.query,
            scenario_type=case.scenario_type,
            ndcg_at_10=round(score, 4),
            gate_status="pass",
            relaxed_field=relaxed_field,
            system_ranking=system_ranking,
            system_relevance=system_relevance,
        ))

    avg = float(np.mean([r.ndcg_at_10 for r in results])) if results else 0.0

    # Break down average NDCG by scenario type
    by_scenario: dict[str, list[float]] = {}
    for r in results:
        by_scenario.setdefault(r.scenario_type, []).append(r.ndcg_at_10)
    by_scenario_avg = {k: round(float(np.mean(v)), 4) for k, v in by_scenario.items()}

    return EvaluationReport(
        avg_ndcg_at_10=round(avg, 4),
        per_case=results,
        by_scenario=by_scenario_avg,
    )


# ── Sample test suite (fill in ground_truth after running once) ───────────────

def build_sample_test_suite() -> list[TestCase]:
    """
    Starter test suite covering the three scenario types.
    ground_truth values are placeholders (all 0) — replace them with real
    relevance labels after inspecting actual pipeline output.

    How to label:
      1. Run evaluate_test_suite() once — inspect system_ranking per case.
      2. Open the DB, look up each laptop_id, judge fit against the query.
      3. Assign 3 / 1 / 0 and paste the ids into ground_truth below.
      4. Re-run — now NDCG reflects real quality.
    """
    return [
        # Scenario 1: Normal — reasonable constraints, direct retrieval should work
        TestCase(
            query="lightweight laptop for university student, budget RM 3500",
            constraints=UserConstraints(
                budget=3500,
                weight_limit=1.5,
                purpose=["Office"],
            ),
            ground_truth={},   # fill in after first run
            scenario_type="normal",
            description="Standard student query — should pass gate directly",
        ),
        TestCase(
            query="gaming laptop with RTX 4060 under RM 6000",
            constraints=UserConstraints(
                budget=6000,
                purpose=["Gaming"],
            ),
            ground_truth={},
            scenario_type="normal",
            description="Gaming query with realistic budget",
        ),

        # Scenario 2: Relaxation — constraint combo has no catalog solution
        TestCase(
            query="gaming laptop under RM 3000",
            constraints=UserConstraints(
                budget=3000,
                purpose=["Gaming"],
            ),
            ground_truth={},
            scenario_type="relaxation",
            description="Gaming + RM 3000 has no match — triggers budget relaxation",
        ),
        TestCase(
            query="lightweight programming laptop under 1.2kg, budget RM 4000",
            constraints=UserConstraints(
                budget=4000,
                weight_limit=1.2,
                purpose=["Programming"],
            ),
            ground_truth={},
            scenario_type="relaxation",
            description="1.2kg is very strict — triggers weight relaxation first",
        ),

        # Scenario 3: Gating — constraint is so niche even relaxation can't help
        TestCase(
            query="MacBook Pro with 96GB RAM under RM 2000",
            constraints=UserConstraints(
                budget=2000,
                brand_preferences=["apple"],
            ),
            ground_truth={},
            scenario_type="gating",
            description="Impossible combo — gate should fire, NDCG expected 0.0",
        ),
    ]


# ── Live pipeline logging (called on every real user request) ─────────────────

def log_pipeline_result(
    gate: GateResult,
    query: str,
    relaxation=None,          # RelaxationResult | None
    user_id=None,             # uuid.UUID | None
    conversation_id=None,     # uuid.UUID | None
    session: Session = None,  # type: ignore[assignment]
) -> None:
    """
    Persist pipeline metrics for every live user request.

    Two destinations:
      1. DB table `pipeline_eval_logs` — queryable, joins to users/conversations
      2. logs/eval/pipeline_trace.jsonl — one JSON line per run, easy to grep
         in CI or tail in production

    Called after the gate decision so we always have the final outcome.
    Never raises — logging failure must never break the user's response.
    """
    from datetime import datetime, timezone
    from app.conversations.models import PipelineEvalLog

    result_ids = [c.laptop_id for c in gate.candidates] if gate.candidates else []

    record = {
        "trace_id": str(uuid.uuid4()),
        "user_id": str(user_id) if user_id else None,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "query": query,
        "gate_status": gate.status,
        "top_score": gate.top_score,
        "relaxed_field": relaxation.relaxed_field if relaxation else None,
        "relaxed_from": relaxation.original_value if relaxation else None,
        "relaxed_to": relaxation.relaxed_value if relaxation else None,
        "bottleneck": gate.bottleneck,
        "candidate_count": len(result_ids),
        "result_laptop_ids": result_ids,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # 1. File log (always runs, even without a DB session)
    try:
        _eval_file_logger.info(json.dumps(record, ensure_ascii=False))
    except Exception:
        pass

    # 2. DB log (only when a session is provided)
    if session is None:
        return
    try:
        session.add(PipelineEvalLog(
            user_id=user_id,
            conversation_id=conversation_id,
            query=query,
            gate_status=gate.status,
            top_score=gate.top_score,
            relaxed_field=record["relaxed_field"],
            relaxed_from=record["relaxed_from"],
            relaxed_to=record["relaxed_to"],
            bottleneck=gate.bottleneck,
            candidate_count=len(result_ids),
            result_laptop_ids=result_ids,
        ))
        session.commit()
    except Exception:
        session.rollback()  # never let a logging failure break the response
