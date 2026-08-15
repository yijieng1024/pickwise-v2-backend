"""
Ask resolve_benchmark directly what it picks, and show why.

FX607VU scored gpu = 0.0. `_score_gpu` has three exits -- Apple proxy, a
resolved mark, or 50.0 when unresolved -- and none of them returns 0.0. So the
lookup SUCCEEDED and normalized to zero, meaning it resolved to a part whose
mark equals the catalog minimum (81).

This prints what resolve_benchmark actually returns for the catalog string and
for the same string without the vendor prefix, then ranks the benchmark table
under several RapidFuzz scorers so the winning row is visible rather than
inferred.

Usage (Windows PowerShell, from the repo root):
    python scripts\\probe_gpu_resolution.py
    python scripts\\probe_gpu_resolution.py --query "NVIDIA GeForce RTX 4050 Laptop GPU"
"""
import argparse
import sys

from rapidfuzz import fuzz, process
from sqlmodel import Session, select

from app.database import engine
from app.benchmark.model import GPUBenchmark
from app.pickscore.engine import resolve_benchmark

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RULE = "=" * 70

# The catalog string that produced gpu = 0.0, and the same string with the
# vendor prefix removed. If these resolve differently, the 56 rows carrying
# the "NVIDIA " prefix are all affected.
DEFAULT_QUERIES = [
    "NVIDIA GeForce RTX 4050 Laptop GPU",
    "GeForce RTX 4050 Laptop GPU",
    "GeForce RTX 5060 Laptop GPU",      # the partial mis-resolution, for contrast
]

# token_set_ratio is the suspected culprit: it scores a strict subset as a
# perfect match, so a shorter, less specific row ties with -- or beats -- the
# exact one. Running several scorers side by side shows which one reproduces
# the behaviour actually observed.
SCORERS = [
    ("token_set_ratio", fuzz.token_set_ratio),
    ("token_sort_ratio", fuzz.token_sort_ratio),
    ("WRatio", fuzz.WRatio),
    ("ratio", fuzz.ratio),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--query", action="append", default=None,
                   help="GPU string to probe. Repeatable.")
    p.add_argument("--top", type=int, default=8,
                   help="How many candidates to show per scorer.")
    args = p.parse_args()

    queries = args.query or DEFAULT_QUERIES

    with Session(engine) as session:
        rows = session.exec(select(GPUBenchmark)).all()

    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in rows]
    names = [n for n, _ in gpu_benchmarks]
    mark_by_name = {n: m for n, m in gpu_benchmarks}

    marks = [m for _, m in gpu_benchmarks if m is not None]
    print(RULE)
    print(f"GPU benchmark table: {len(gpu_benchmarks)} rows, "
          f"marks {min(marks)} .. {max(marks)}")
    print(RULE)
    print()

    for q in queries:
        print(RULE)
        print(f"QUERY: {q!r}")
        print(RULE)

        # 1. What the engine itself decides. This is the authoritative answer;
        #    everything below only explains it.
        try:
            result = resolve_benchmark(q, gpu_benchmarks)
            print("  resolve_benchmark ->")
            if isinstance(result, dict):
                for k, v in result.items():
                    print(f"      {k}: {v}")
            else:
                print(f"      {result}")
        except Exception as e:
            print(f"  resolve_benchmark raised {type(e).__name__}: {e}")
        print()

        # 2. Why. Each scorer's ranking of the same table.
        for label, scorer in SCORERS:
            print(f"  --- {label} ---")
            try:
                hits = process.extract(q, names, scorer=scorer, limit=args.top)
            except Exception as e:
                print(f"      failed: {type(e).__name__}: {e}")
                continue
            for name, score, _idx in hits:
                mark = mark_by_name.get(name)
                print(f"      {score:6.1f}  {str(mark):>7}  {name}")
            print()

    print(RULE)
    print("Read it like this:")
    print("  - If resolve_benchmark's mark is the table/catalog minimum, the")
    print("    lookup succeeded onto a near-worthless part -- a wrong match,")
    print("    not a miss.")
    print("  - If several rows tie at 100 under token_set_ratio, the winner is")
    print("    decided by table order, which is why a generic name can beat an")
    print("    exact one.")
    print("  - If the two RTX 4050 strings resolve differently, the vendor")
    print("    prefix is load-bearing and all 56 prefixed rows are affected.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())