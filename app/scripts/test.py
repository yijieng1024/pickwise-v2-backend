"""
Find catalog GPU/CPU strings that resolve to an implausible benchmark row.

The engine's matcher lives in Python, so SQL cannot answer "which laptop
匹到了那個 mark 4 的行". This walks every active laptop's gpu_model /
processor_model through resolve_benchmark and prints anything suspicious:
  - a resolved score below a floor no 2024+ laptop part could sit at
  - a string with no model-number token (nothing to anchor the match on)
  - a confidence in the 0.85-0.90 band, i.e. only just accepted

Usage:
    python -m app.scripts.audit_benchmark_matches
    python -m app.scripts.audit_benchmark_matches --all-statuses
"""
import argparse
from collections import defaultdict

from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.database import engine
from app.benchmark.model import CPUBenchmark, GPUBenchmark

# Same mapper-registration chain the dump script needs (Laptop ->
# LaptopCustomization -> Category are string-name relationships).
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.laptop_models import Laptop
from app.pickscore.benchmark_service import resolve_benchmark, _normalize

# No 2024+ laptop part scores anywhere near these. Anything below is a
# mismatch onto the pre-2005 tail of the PassMark table.
GPU_FLOOR = 500
CPU_FLOOR = 2000

SEP = "=" * 78


def anchor_tokens(model_string: str) -> list[str]:
    """
    Model-number tokens: the only thing that ties a query to a specific part.
    "GeForce RTX 5060 Laptop GPU" -> ['5060']; "AMD Radeon Graphics" -> [].
    A string with none of these cannot be matched to a specific part by any
    scorer — whatever it lands on is the nearest-looking row, not the right one.
    """
    needle = _normalize(model_string).replace("-", " ")
    return [t for t in needle.split()
            if any(c.isdigit() for c in t) and len(t) >= 3]


def audit(session, table_label, column, benchmarks, floor, active_only):
    stmt = sa_select(Laptop.model_code, column)
    if active_only:
        stmt = stmt.where(Laptop.status == "active")
    rows = session.execute(stmt).all()

    # One entry per distinct model string — resolve_benchmark is cached and
    # per-string anyway, and 276 rows collapse to well under 130 strings.
    by_string = defaultdict(list)
    for code, model in rows:
        if model:
            by_string[model].append(code)

    low, unanchored, borderline, ok = [], [], [], 0

    for model, codes in sorted(by_string.items()):
        result = resolve_benchmark(model, benchmarks)
        score = result["score"]
        conf = result["match_confidence"]
        tokens = anchor_tokens(model)
        entry = (model, score, conf, len(codes), codes[:3])

        if not tokens:
            unanchored.append(entry)
        elif score is not None and score < floor:
            low.append(entry)
        elif score is not None and conf < 0.90:
            borderline.append(entry)
        else:
            ok += 1

    print(SEP)
    print("{}  —  {} distinct strings across {} laptop rows".format(
        table_label, len(by_string), len(rows)))
    print(SEP)

    def show(title, items, note):
        print("\n  {} ({})".format(title, len(items)))
        print("  {}".format(note))
        if not items:
            print("    (none)")
            return
        for model, score, conf, n, codes in sorted(items, key=lambda e: (e[1] is None, e[1] or 0)):
            print("    {:<48} -> {:<8} conf {:<6} x{}".format(
                model[:48], score if score is not None else "None",
                round(conf, 3), n))
            print("      {}".format(", ".join(codes)))

    show("RESOLVED BELOW FLOOR (< {})".format(floor), low,
         "Matched onto the pre-2005 tail. These set the range minimum.")
    show("NO ANCHOR TOKEN", unanchored,
         "No model number to match on. Whatever they resolved to is arbitrary.")
    show("ACCEPTED AT 0.85-0.90", borderline,
         "Only just cleared the threshold — verify each one by hand.")
    print("\n  Clean: {}\n".format(ok))

def desktop_collisions(session, gpu_benchmarks, active_only=True):
    """
    Catalog strings that name a desktop part where the machine has the mobile one.

    "GeForce RTX 5090" resolves to the desktop row at high confidence — the
    matcher is doing exactly what the string asks. The only way to spot it is
    to ask whether a "Laptop GPU" row for the same model number also exists;
    if it does, a laptop catalog row naming the bare model is almost certainly
    the wrong part. Confidence cannot detect this, which is why it survived
    every threshold change.
    """
    stmt = sa_select(Laptop.model_code, Laptop.gpu_model)
    if active_only:
        stmt = stmt.where(Laptop.status == "active")
    rows = session.execute(stmt).all()

    counts = defaultdict(list)
    for code, gpu in rows:
        if gpu:
            counts[gpu].append(code)

    print(SEP)
    print("DESKTOP/LAPTOP COLLISIONS")
    print(SEP)

    found = 0
    for model in sorted(counts):
        key = _normalize(model)
        if "laptop" in key:
            continue
        tokens = [t for t in key.replace("-", " ").split()
                  if any(c.isdigit() for c in t) and len(t) >= 3]
        if not tokens:
            continue

        laptop_rows = [(n, m) for n, m in gpu_benchmarks
                       if "laptop" in n.lower()
                       and all(t in _normalize(n).replace("-", " ") for t in tokens)]
        if not laptop_rows:
            continue

        found += 1
        got = resolve_benchmark(model, gpu_benchmarks)
        print("\n  {:<44} -> {:<8} x{}".format(
            model[:44], got["score"], len(counts[model])))
        print("      {}".format(", ".join(counts[model][:4])))
        for n, m in sorted(laptop_rows, key=lambda r: r[1]):
            print("      laptop variant: {:<40} {}".format(n[:40], m))

    print("\n  Collisions: {}\n".format(found))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-statuses", action="store_true",
                        help="Include suspended/inactive laptops.")
    args = parser.parse_args()

    with Session(engine) as session:
        cpu_benchmarks = [(r.cpu_name, r.cpu_mark)
                          for r in session.exec(select(CPUBenchmark)).all()]
        gpu_benchmarks = [(r.gpu_name, r.gpu_mark)
                          for r in session.exec(select(GPUBenchmark)).all()]

        audit(session, "GPU", Laptop.gpu_model, gpu_benchmarks,
              GPU_FLOOR, not args.all_statuses)
        audit(session, "CPU", Laptop.processor_model, cpu_benchmarks,
              CPU_FLOOR, not args.all_statuses)
        desktop_collisions(session, gpu_benchmarks, not args.all_statuses)

if __name__ == "__main__":
    main()