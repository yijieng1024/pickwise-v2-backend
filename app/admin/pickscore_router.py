"""
GET /admin/pickscore/summary: everything the admin Pick Score page shows, in
one request and one session.

The page answers: are the scores right and fresh, which laptops have a problem
and why? Everything is read-only. The closest PassMark rows are a diagnosis
(benchmark_service.closest_benchmarks) and never change how a laptop is
matched; the proxy kinds are recomputed with the engine's own
resolve_gpu_benchmark against today's table, so after a benchmark change they
can differ from what the stored scores used until the next recalculation.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app.admin.dashboard_router import _utc, pickscore_counts
from app.database import get_session
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopPickScore, LaptopStatus
from app.laptops.pickscore_adapter import get_scoring_data
from app.laptops.pickscore_general import USE_CASE_PRIORITIES, get_ranking_for_use_case
from app.pickscore.benchmark_service import (
    CPU_CONFIDENCE_THRESHOLD,
    closest_benchmarks,
    gpu_diagnosis,
    resolve_gpu_benchmark,
)
from app.users.auth import get_current_admin

router = APIRouter(
    prefix="/admin/pickscore",
    tags=["Admin - Pick Score"],
    dependencies=[Depends(get_current_admin)],
)

_PROBLEM_LIST = 50
_TOP = 10
BUCKETS = ["0–9", "10–19", "20–29", "30–39", "40–49", "50–59", "60–69", "70–79", "80–89", "90+"]


def bucket_counts(scores: list[int]) -> list[int]:
    """10-point buckets; 90+ holds 90–100."""
    counts = [0] * len(BUCKETS)
    for s in scores:
        counts[min(int(s) // 10, len(BUCKETS) - 1)] += 1
    return counts


def weight_shares(priorities: dict[str, dict[str, float]]) -> dict:
    """Each factor's base weight and its real share in general mode, where the
    purpose and portability modifiers are neutral: weight / total."""
    return {
        use_case: {f: {"weight": w, "share": round(w / sum(weights.values()), 3)} for f, w in weights.items()}
        for use_case, weights in priorities.items()
    }


@router.get("/summary")
def pickscore_summary(session: Session = Depends(get_session)) -> dict:
    # First: a cold build (no cached copy yet) commits the session. Never
    # force_refresh here; a request must not pay for the build.
    _, cpu_benchmarks, gpu_benchmarks = get_scoring_data(session)

    active = Laptop.status == LaptopStatus.ACTIVE.value
    scored, generated, unverified = pickscore_counts(session)
    recalculated_at = session.exec(select(func.max(LaptopPickScore.updated_at))).one()
    catalog_changed_at = session.exec(select(func.max(Laptop.updated_at))).one()
    edited_since = (
        session.exec(select(func.count(Laptop.id)).where(Laptop.updated_at > recalculated_at)).one()
        if recalculated_at else None
    )

    laptops = session.exec(
        select(Laptop, LaptopBrand.name)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)  # type: ignore[arg-type]
        .where(active)
    ).all()
    proxy = Counter(
        resolve_gpu_benchmark(l.gpu_model or "", l.processor_model or "", gpu_benchmarks)["resolution"]
        for l, _ in laptops
    )

    # Every active configuration's stored rows: the distribution, and the
    # union of flags per laptop for the problem list.
    rows = session.exec(
        select(LaptopPickScore.laptop_id, LaptopPickScore.use_case, LaptopPickScore.score, LaptopPickScore.flags)
        .join(Laptop, Laptop.id == LaptopPickScore.laptop_id)  # type: ignore[arg-type]
        .where(active)
    ).all()
    scores: dict[str, list[int]] = defaultdict(list)
    withheld_rows: Counter = Counter()
    flags_of: dict = defaultdict(dict)
    for laptop_id, use_case, score, flags in rows:
        if score is None:
            withheld_rows[use_case] += 1
        else:
            scores[use_case].append(score)
        for k in ("score_withheld", "cpu_benchmark_unresolved", "gpu_benchmark_unresolved"):
            if (flags or {}).get(k):
                flags_of[laptop_id][k] = True

    problems = sorted(
        ((l, b, flags_of[l.id]) for l, b in laptops if flags_of.get(l.id)),
        key=lambda t: (not t[2].get("score_withheld"), t[0].product_name, str(t[0].id)),
    )
    items = []
    for l, brand_name, f in problems[:_PROBLEM_LIST]:
        cpu_bad, gpu_bad = bool(f.get("cpu_benchmark_unresolved")), bool(f.get("gpu_benchmark_unresolved"))
        items.append({
            "laptop_id": l.id,
            "product_name": l.product_name,
            "brand_name": brand_name,
            "status": l.status,
            "withheld": bool(f.get("score_withheld")),
            "cpu": {
                "catalog": l.processor_model,
                "unresolved": cpu_bad,
                "diagnosis": closest_benchmarks(l.processor_model or "", cpu_benchmarks, CPU_CONFIDENCE_THRESHOLD)
                if cpu_bad else None,
            },
            "gpu": {
                "catalog": l.gpu_model,
                "unresolved": gpu_bad,
                "diagnosis": gpu_diagnosis(l.gpu_model or "", l.processor_model or "", gpu_benchmarks)
                if gpu_bad else None,
            },
        })

    top = {}
    for use_case in USE_CASE_PRIORITIES:
        top[use_case] = [
            {
                "rank": i + 1,
                "laptop_id": laptop.id,
                "product_name": laptop.product_name,
                "brand_name": brand_name,
                "processor_model": laptop.processor_model,
                "gpu_model": laptop.gpu_model,
                "score": row.score,
                "gpu_score_is_proxy": bool(row.flags.get("gpu_score_is_proxy")),
            }
            for i, (row, laptop, brand_name) in enumerate(get_ranking_for_use_case(session, use_case, _TOP))
        ]

    return {
        "generated_at": datetime.now(timezone.utc),
        "status": {
            "recalculated_at": _utc(recalculated_at),
            # Amber when recalculated_at is older than this (same rule as the Dashboard).
            "catalog_changed_at": _utc(catalog_changed_at),
            "edited_since": edited_since,
            "active": len(laptops),
            "scored": scored,
            "missing": len(laptops) - generated,
            "withheld": generated - scored,
            "unverified": unverified,
        },
        # Over active laptops, recomputed now: direct / laptop_variant /
        # apple_equivalent / integrated / unresolved.
        "proxy": {
            "integrated": proxy["integrated"],
            "apple_equivalent": proxy["apple_equivalent"],
            "unresolved": proxy["unresolved"],
        },
        "problems": {"total": len(problems), "limit": _PROBLEM_LIST, "items": items},
        "distribution": {
            "buckets": BUCKETS,
            "by_use_case": {
                uc: {"counts": bucket_counts(scores[uc]), "withheld": withheld_rows[uc]}
                for uc in USE_CASE_PRIORITIES
            },
        },
        "top": top,
        "weights": weight_shares(USE_CASE_PRIORITIES),
    }
