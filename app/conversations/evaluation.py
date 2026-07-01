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
            ground_truth={
                "807d1c28-392e-469e-bf39-e98dfd453984": 3,  # ExpertBook P1 RM3399 1.4kg
                "615314c7-c246-424b-b17b-c627e25a9746": 3,  # ExpertBook P1 RM3399 1.4kg (680M)
                "4bccb37c-2b49-4184-9e72-a90157428f0b": 1,  # ExpertBook BM1 1.4kg, price unknown
                "7df2a1f6-bfa2-4d7a-97e1-6b3a505c5b99": 1,  # ExpertBook P3 1.4kg, price unknown
                "38246257-e855-4528-a78d-7ca7fe07f1c9": 1,  # ExpertBook BM1 1.4kg, price unknown
                "fdfcc655-d064-4ed8-afa8-e04b4c7142f2": 3,  # Vivobook 14 RM3399 1.5kg
                "bd6ec516-2ef3-401f-8555-147b07830801": 1,  # Vivobook 14 Copilot+ 1.5kg, price unknown
                "ee2f980b-0451-4cce-a00e-e3531dbffc54": 3,  # MacBook Neo RM2899 1.2kg 512GB
                "abc2df6f-3150-4652-bbc5-c40e3f2c670d": 3,  # MacBook Neo RM2499 1.23kg 256GB
                "b2db4709-7c1b-425c-b7a5-93a7345f9051": 1,  # Vivobook S14 1.4kg, price unknown
                "8d422c74-066f-4c56-9448-0e03a1a68088": 1,  # ExpertBook B3 1.4kg, price unknown
            },
            scenario_type="normal",
            description="Standard student query — should pass gate directly",
        ),
        TestCase(
            query="gaming laptop with RTX 4060 under RM 6000",
            constraints=UserConstraints(
                budget=6000,
                purpose=["Gaming"],
            ),
            ground_truth={
                "e0d50667-eab3-4f4c-a259-ae341332af23": 1,  # Gaming V16 RTX 5060, price unknown
                "8682ef7c-fb2d-4ce6-b162-3582a795b6de": 3,  # TUF F16 RTX 5060 RM5899
                "515775ce-7329-44ce-9c17-03b74f336c21": 0,  # Gaming V16 RTX 4050, weaker GPU
                "71be7e97-ac43-405a-88a6-6e1e8cedbca6": 3,  # TUF F16 RTX 5060 RM5899
                "7f45dc93-e101-4896-a5c7-fda68448c721": 1,  # Gaming V16 RTX 5050, price unknown
                "e534245d-2e6c-41da-8835-f485d170a539": 1,  # ROG Strix G18 RTX 5060, price unknown
                "1b8d5dee-4984-4e1e-9bf3-94d082db8675": 3,  # TUF F16 RTX 5060 RM5899
                "7dc36c93-1dff-46b0-b11a-f87722bc975d": 0,  # Gaming V16 RTX 3050, too weak
                "5d141b2b-9758-4a90-909a-3f379264465e": 1,  # ROG Strix G18 2026 RTX 5060, price unknown
                "9c0face8-9041-42f6-9bf5-db5e728f0958": 1,  # ProArt PX13 RTX 4060, price unknown
                "ac65237e-eda0-4520-bda1-1dc3e812b5e3": 3,  # TUF F16 RTX 5070 RM5899, better than asked
                "f45661fa-3e75-4f42-964f-04622eba6ffc": 1,  # TUF F16 RTX 5050 RM5899, slightly weaker
                "d1d64e63-2561-4936-91b1-c2fd70839950": 1,  # TUF F16 RTX 5050 RM5899, slightly weaker
                "f5b5e2c6-15a2-4cf3-b5bf-ad5d22b10caa": 0,  # Vivobook S16 Intel UHD, not gaming
                "b1b3d46a-e177-43e9-bde0-4378c702ce74": 0,  # Vivobook S16 Intel UHD, not gaming
                "61f1b315-8b3f-46ec-b972-fe6e20f3941c": 0,  # Vivobook S16 Intel, not gaming
                "baa9afc7-b83b-40cb-b398-53bcfbe93679": 0,  # Vivobook S16 AMD Radeon, not gaming
            },
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
            ground_truth={
                # Gaming laptops — right category but budget unverifiable (price_rm=0)
                "7dc36c93-1dff-46b0-b11a-f87722bc975d": 1,  # Gaming V16 RTX 3050
                "7f45dc93-e101-4896-a5c7-fda68448c721": 1,  # Gaming V16 RTX 5050
                "e0d50667-eab3-4f4c-a259-ae341332af23": 1,  # Gaming V16 RTX 5060
                "515775ce-7329-44ce-9c17-03b74f336c21": 1,  # Gaming V16 RTX 4050
                "7e267236-042a-4f36-b181-905435c79b81": 1,  # ROG Strix G18 RTX 5070
                "5d141b2b-9758-4a90-909a-3f379264465e": 1,  # ROG Strix G18 2026 RTX 5060
                "e534245d-2e6c-41da-8835-f485d170a539": 1,  # ROG Strix G18 RTX 5060
                "b95d8df4-ea51-4216-9afb-cbb24f82b1aa": 1,  # ROG Strix SCAR RTX 5090
                "37aafc85-ccb8-4f09-b491-4e384d0d7d49": 1,  # ROG Strix G16 RTX 5060
                "26efeeb2-50e6-4427-9eb2-a8b7a3144396": 1,  # ROG Strix G16 RTX 5080
                # Non-gaming laptops with known prices that may surface after relaxation
                "ee2f980b-0451-4cce-a00e-e3531dbffc54": 0,  # MacBook Neo — not gaming
                "abc2df6f-3150-4652-bbc5-c40e3f2c670d": 0,  # MacBook Neo 256GB — not gaming
            },
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
            ground_truth={
                # Labelled from previous run where gate passed; gated runs score 0 automatically
                "cc100fd1-f426-49be-abbd-11b5a37dc54d": 3,  # ExpertBook Ultra 1.0kg
                "e6151d48-97cb-407f-8d66-4f030b0d17e1": 3,  # Zenbook A16 1.2kg 48GB
                "51d3868f-94e4-4896-b15b-2b290726a1de": 3,  # ExpertBook Ultra 1.0kg 16GB
                "41113426-e913-45c6-803c-2283285a59b5": 3,  # ExpertBook Ultra 1.0kg 32GB
                "2a26086b-1069-493b-9d03-358f716b13ad": 3,  # ExpertBook Ultra 1.0kg 64GB
                "760370af-6837-43dc-a7ef-1b012a4e070f": 3,  # Zenbook A16 1.1kg 16GB
                "667a0833-964c-4c02-9ce5-3841d6c7ac1e": 3,  # ExpertBook P5 1.2kg
                "59b234f5-c4e6-4a37-bbd2-4c12643ecece": 3,  # ProArt PZ14 0.8kg
                "615314c7-c246-424b-b17b-c627e25a9746": 1,  # ExpertBook P1 1.4kg (only after relaxation)
                "807d1c28-392e-469e-bf39-e98dfd453984": 1,  # ExpertBook P1 1.4kg (only after relaxation)
            },
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
            ground_truth={
                # All results are irrelevant — wrong brand, impossible spec
                # ground_truth left empty intentionally: any returned laptop_id maps to 0
            },
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
