"""
Emit the CPU->integrated-GPU worklist, and validate existing map entries.

Part 1 lists every CPU model that ships with an anchorless GPU string,
ordered by how many laptops it covers, so the map can be filled highest-
impact first rather than all of them at once.

Part 2 checks that each value already in _INTEGRATED_GPU_BY_CPU actually
resolves in the GPU benchmark table -- a typo there fails silently, landing
back on the same fuzzy match this whole change exists to avoid.

Usage:
    python -m app.scripts.audit_integrated_gpu
    python -m app.scripts.audit_integrated_gpu --validate-only
    python -m app.scripts.audit_integrated_gpu --all-statuses
"""
import argparse
import sys
from collections import Counter, defaultdict

from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.database import engine
from app.benchmark.model import GPUBenchmark

# Mapper registration for a standalone script: Laptop -> LaptopCustomization ->
# Category is a chain of string-name relationships whose targets are only
# imported under TYPE_CHECKING (same chain dump_pickscore.py imports).
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.pickscore.benchmark_service import (
    has_anchor_token,
    resolve_benchmark,
    _APPLE_GPU_EQUIVALENT,
    _APPLE_KEY,
    _INTEGRATED_GPU_BY_CPU,
    _integrated_gpu_for,
    _laptop_variant,
    _normalize,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SEP = "=" * 78


def worklist(session, active_only=True):
    stmt = (
        sa_select(Laptop.processor_model, Laptop.gpu_model, LaptopBrand.name)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
    )
    if active_only:
        stmt = stmt.where(Laptop.status == LaptopStatus.ACTIVE.value)
    rows = session.execute(stmt).all()

    counts = Counter()
    gpu_strings = defaultdict(set)
    apple_gpu_counts = Counter()
    for cpu, gpu, brand in rows:
        if not (cpu and gpu) or has_anchor_token(gpu):
            continue
        # Apple's strings are anchorless too, but the core count identifies the
        # part, so they key on the GPU string via _APPLE_GPU_EQUIVALENT rather
        # than on the CPU. Listed separately, not skipped: the brand
        # short-circuit that used to make them unreachable is gone (ADR-0011),
        # so a new Apple GPU with no entry must show up here.
        if (brand or "").lower() == "apple":
            apple_gpu_counts[gpu] += 1
            continue
        counts[cpu] += 1
        gpu_strings[cpu].add(gpu)

    total = sum(counts.values())

    print(SEP)
    print("Apple GPU strings  -  {} strings, {} laptops"
          .format(len(apple_gpu_counts), sum(apple_gpu_counts.values())))
    print(SEP)
    print("  {:<28}{:>6}  {}".format("gpu_model", "count", "mapped to"))
    apple_done = 0
    for gpu, n in apple_gpu_counts.most_common():
        mapped = _APPLE_GPU_EQUIVALENT.get(_normalize(gpu).translate(_APPLE_KEY))
        if mapped:
            apple_done += n
        print("  {:<28}{:>6}  {}".format(gpu[:28], n, mapped or "-- MISSING --"))
    print("\n  Covered: {} / {} laptops\n"
          .format(apple_done, sum(apple_gpu_counts.values())))

    done = 0
    for cpu, n in counts.most_common():
        mapped = _integrated_gpu_for(cpu)
        if mapped:
            done += n
        print("  {:<46}{:>6}  {}".format(
            cpu[:46], n, mapped or "-- MISSING --"))
        # Two spellings under one CPU are scraper variants, not two parts —
        # worth seeing together, since one map entry covers both.
        print("      gpu strings: {}".format(", ".join(sorted(gpu_strings[cpu]))))

    print("\n  Covered: {} / {} laptops\n".format(done, total))


def variant_rewrites(session, gpu_benchmarks, active_only=True):
    """Show what the laptop-variant rule does before trusting it in scoring."""
    stmt = sa_select(Laptop.model_code, Laptop.gpu_model)
    if active_only:
        stmt = stmt.where(Laptop.status == LaptopStatus.ACTIVE.value)

    seen = defaultdict(list)
    for code, gpu in session.execute(stmt).all():
        if gpu:
            seen[gpu].append(code)

    print(SEP)
    print("LAPTOP-VARIANT REWRITES")
    print(SEP)
    rewritten = 0
    for model in sorted(seen):
        key = _normalize(model)
        if "laptop" in key:
            continue
        variant = _laptop_variant(key, gpu_benchmarks)
        if not variant:
            continue
        rewritten += len(seen[model])
        before = resolve_benchmark(model, gpu_benchmarks)["score"]
        after = resolve_benchmark(variant, gpu_benchmarks)["score"]
        print("  {:<34} {:>7} -> {:<7} x{}  ({})".format(
            model[:34], before, after, len(seen[model]), variant))
    print("\n  {} laptops rewritten\n".format(rewritten))


def validate(gpu_benchmarks):
    tables = (
        ("_INTEGRATED_GPU_BY_CPU", _INTEGRATED_GPU_BY_CPU),
        ("_APPLE_GPU_EQUIVALENT", _APPLE_GPU_EQUIVALENT),
    )
    total = sum(len(t) for _, t in tables)
    print(SEP)
    print("Validating {} map values against the GPU benchmark table".format(total))
    print(SEP)
    bad = 0
    for label, table in tables:
        print("\n  {} ({})".format(label, len(table)))
        if not table:
            print("    (empty)")
            continue
        for key, target in sorted(table.items()):
            r = resolve_benchmark(target, gpu_benchmarks)
            ok = r.get("matched_name") == target
            bad += 0 if ok else 1
            print("    {}  {:<34} -> {:<34} {:<8} conf {}".format(
                "OK " if ok else "!! ", key, target,
                r["score"] if r["score"] is not None else "None",
                round(r["match_confidence"], 3)))
    print("\n  {} entries failed\n".format(bad))

def check_apple_ladder(gpu_benchmarks):
    """
    Core count and resolved mark must move together. A name-resolution check
    cannot catch a wrong anchor -- both entries resolved at conf 1.0 while the
    8-core scored 47% above the 10-core, because the anchors were chosen on
    Steel Nomad and PassMark G3D ranks those two parts differently.
    """
    import re
    ladder = []
    for key, target in _APPLE_GPU_EQUIVALENT.items():
        m = re.match(r"(\d+) core gpu", key)
        if not m:
            continue
        score = resolve_benchmark(target, gpu_benchmarks)["score"]
        if score is not None:
            ladder.append((int(m.group(1)), key, target, score))

    ladder.sort()
    print(SEP)
    print("APPLE LADDER MONOTONICITY")
    print(SEP)
    bad = 0
    prev = None
    for cores, key, target, score in ladder:
        flag = "   "
        if prev is not None and score <= prev[3]:
            flag = "!! "
            bad += 1
        print("  {}{:>3}-core  {:<38} {}".format(flag, cores, target, score))
        if flag == "!! ":
            print("      lower than {}-core ({})".format(prev[0], prev[3]))
        prev = (cores, key, target, score)
    print("\n  {} inversions\n".format(bad))

def check_ambiguous_targets(gpu_benchmarks):
    """
    A target name that is a prefix of another benchmark row is ambiguous even
    at confidence 1.0: "Intel Arc 140T" and "Intel Arc 140T GPU" are the same
    silicon with marks 17% apart, and WRatio happily returns either.
    """
    targets = set(_INTEGRATED_GPU_BY_CPU.values()) | set(_APPLE_GPU_EQUIVALENT.values())
    print(SEP)
    print("AMBIGUOUS TARGET NAMES")
    print(SEP)
    bad = 0
    for target in sorted(targets):
        key = _normalize(target)
        siblings = [(n, m) for n, m in gpu_benchmarks
                    if _normalize(n) != key and _normalize(n).startswith(key)]
        if not siblings:
            continue
        bad += 1
        own = resolve_benchmark(target, gpu_benchmarks)["score"]
        print("  !! {:<38} -> {}".format(target, own))
        for n, m in sorted(siblings, key=lambda r: r[1]):
            print("       also in table: {:<34} {}".format(n[:34], m))
    print("\n  {} ambiguous targets\n".format(bad))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true",
                        help="Skip the worklist; only re-check the map values.")
    parser.add_argument("--rewrites", action="store_true",
                        help="Only show what the laptop-variant rule rewrites.")
    parser.add_argument("--all-statuses", action="store_true",
                        help="Include suspended/inactive laptops in the worklist.")
    args = parser.parse_args()

    with Session(engine) as session:
        gpu_benchmarks = [(r.gpu_name, r.gpu_mark)
                          for r in session.exec(select(GPUBenchmark)).all()]
        if args.rewrites:
            variant_rewrites(session, gpu_benchmarks,
                             active_only=not args.all_statuses)
            return
        if not args.validate_only:
            worklist(session, active_only=not args.all_statuses)
            variant_rewrites(session, gpu_benchmarks,
                             active_only=not args.all_statuses)
        validate(gpu_benchmarks)
        check_ambiguous_targets(gpu_benchmarks)


if __name__ == "__main__":
    main()
