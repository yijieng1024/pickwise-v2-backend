"""
Measure the benchmark match-confidence distribution across the whole catalog,
for both GPU and CPU, before changing CONFIDENCE_THRESHOLD.

Raising the threshold trades wrong matches for unknowns. Which trade is worth
making depends on where the real confidences actually sit, and that is a
measurement, not a guess: if most parts resolve above 0.95 the threshold can
go high for free, but if a band of genuine parts sits at 0.7-0.85 because
their names are written differently, raising it pushes them into the 50.0
fallback and moves every score toward the middle.

Run this AFTER the both-sides normalization fix, otherwise it measures the
broken behaviour.

Usage (Windows PowerShell, from the repo root):
    python app\\scripts\\benchmark_confidence.py
    python app\\scripts\\benchmark_confidence.py --show-below 0.9 --limit 40
"""
import argparse
import sys
from collections import Counter

from sqlmodel import Session, select

from app.database import engine
from app.benchmark.model import CPUBenchmark, GPUBenchmark

# Mapper registration for a standalone script -- same chain as dump-pickscore.
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.laptop_models import Laptop
from app.pickscore.benchmark_service import resolve_benchmark

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RULE = "=" * 72
THRESHOLDS = [0.60, 0.70, 0.80, 0.85, 0.90, 0.95]


def analyse(label: str, pairs: list[tuple[str, str]], table: list[tuple[str, int]],
            show_below: float, limit: int) -> None:
    """pairs is (model_code, model_string) for every catalog row."""
    print(RULE)
    print(f"{label}  --  {len(pairs)} catalog rows")
    print(RULE)

    buckets: Counter = Counter()
    low: list[tuple[float, str, str]] = []
    blank = 0
    # Distinct strings matter more than row count: one badly-named part shared
    # by 14 configurations is one problem, not fourteen.
    seen: dict[str, float] = {}

    for model_code, model_string in pairs:
        if not model_string or not model_string.strip():
            blank += 1
            continue
        result = resolve_benchmark(model_string, table)
        conf = float(result.get("match_confidence") or 0.0)
        buckets[round(conf * 20) / 20] += 1  # 0.05 granularity
        seen[model_string] = conf
        if conf < show_below:
            low.append((conf, model_code, model_string))

    print(f"  blank/missing model string: {blank}")
    print(f"  distinct model strings    : {len(seen)}")
    print()

    print("  Confidence histogram (0.05 buckets, by catalog row):")
    for value in sorted(buckets):
        bar = "#" * min(60, buckets[value])
        print(f"    {value:>4.2f}  {buckets[value]:>4}  {bar}")
    print()

    print("  Rows that would fall to the 50.0 fallback at each threshold:")
    total = sum(buckets.values())
    for t in THRESHOLDS:
        rejected = sum(n for v, n in buckets.items() if v < t)
        pct = (rejected / total * 100) if total else 0
        print(f"    >= {t:.2f}   {rejected:>4} of {total}  ({pct:5.1f}%)")
    print()

    if low:
        print(f"  Distinct strings resolving below {show_below:.2f} "
              f"(worst first, max {limit}):")
        shown = {}
        for conf, model_code, model_string in sorted(low):
            if model_string in shown:
                continue
            shown[model_string] = True
            print(f"    {conf:.3f}  {model_string}")
            if len(shown) >= limit:
                break
        print()
        print("  Read these individually: a real part written differently is")
        print("  worth keeping, a genuine miss is worth rejecting. The split")
        print("  between them is what sets the threshold.")
    else:
        print(f"  Nothing resolves below {show_below:.2f}.")
    print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--show-below", type=float, default=0.90)
    p.add_argument("--limit", type=int, default=30)
    args = p.parse_args()

    with Session(engine) as session:
        laptops = session.exec(select(Laptop)).all()
        cpu_table = [(r.cpu_name, r.cpu_mark)
                     for r in session.exec(select(CPUBenchmark)).all()]
        gpu_table = [(r.gpu_name, r.gpu_mark)
                     for r in session.exec(select(GPUBenchmark)).all()]

    print(RULE)
    print(f"Catalog: {len(laptops)} laptops   "
          f"CPU table: {len(cpu_table)}   GPU table: {len(gpu_table)}")
    print(RULE)
    print()

    analyse(
        "GPU",
        [(str(l.model_code), l.gpu_model or "") for l in laptops],
        gpu_table, args.show_below, args.limit,
    )
    analyse(
        "CPU",
        [(str(l.model_code), l.processor_model or "") for l in laptops],
        cpu_table, args.show_below, args.limit,
    )

    print(RULE)
    print("Pick the threshold from the rejection table, not from a round")
    print("number. Then change CONFIDENCE_THRESHOLD, then re-run")
    print("POST /pick-scores/generate-all -- scores are precomputed into")
    print("LaptopPickScore and will not move until they are regenerated.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())