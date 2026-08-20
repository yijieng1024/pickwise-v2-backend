"""
Compare normalization curves against the live catalog, before ADR-0011 picks one.

The open question from ADR-0006 is that the eight PickScore factors are weighted
as though they shared a scale when their effective spread differs by roughly 5x.
Price is inverted against an RM36,999 ceiling, so the band the catalog actually
sits in (RM3,000-8,000) all scores 81-96; ram_storage measures 16GB against a
128GB workstation and lands at 9.7. Battery and weight are fine, because their
bounds are physical rather than distributional.

Rather than argue about which curve is right, run each one over the real catalog
and read three things off it:

  1. Effective spread per factor (p10..p90 of the resulting raw scores). This is
     the quantity ADR-0006 named and never measured directly.
  2. The five use-case scores for one reference machine, so the inversion is
     visible as a number.
  3. Top-10 ranking per use case and its overlap with today's, because a curve
     that changes no rankings changes nothing a user sees.

Nothing here writes to the database and generate-all is not involved.

Usage:
    python -m app.scripts.simulate_normalization
    python -m app.scripts.simulate_normalization --model-code <code>
    python -m app.scripts.simulate_normalization --curves minmax percentile
"""
import argparse
import bisect
import math
from collections import defaultdict

from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.database import engine as db_engine
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.brand_model import LaptopBrand

# Mapper registration for a standalone script (see dump_pickscore.py).
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.laptop_models import Laptop
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.laptops.pickscore_general import USE_CASE_PRIORITIES
from app.pickscore import engine as pickscore_engine
from app.pickscore.benchmark_service import resolve_benchmark

try:
    from app.pickscore.benchmark_service import resolve_gpu_benchmark
except ImportError:  # pre-ADR-0010 checkout
    resolve_gpu_benchmark = None

SEP = "=" * 78
REFERENCE = "asus-tuf-gaming-f16-i7-14650hx-rtx5060-16gb-1tb"

# Percentile bounds for the clamped curve. p5/p95 rather than p10/p90 because
# clamping saturates everything outside the window to 0 or 100, and 10% of the
# catalog pinned at each end trades one loss of resolution for another.
CLAMP_LO, CLAMP_HI = 5.0, 95.0


def percentile(sorted_values, q):
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


# ---------------------------------------------------------------- curves


def curve_minmax(value, lo, hi, dist):
    """Today's behaviour. Sensitive to exactly two data points."""
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def curve_percentile(value, lo, hi, dist):
    """
    Rank among the catalog rather than position between its extremes.

    Guarantees a full 0-100 spread for every factor, which is exactly what the
    inversion diagnosis calls for. The cost is that a score becomes relative to
    the current catalog: adding one laptop, or suspending one, moves everyone
    else's number even though nothing about those machines changed. That sits
    awkwardly against ADR-0006's positioning of PickScore as a buy indicator —
    an answer to "should I buy this one" should not move because a competitor
    was listed.
    """
    if not dist:
        return 50.0
    below = bisect.bisect_left(dist, value)
    equal = bisect.bisect_right(dist, value) - below
    # Midpoint of the tied block, so identical configurations score identically
    # instead of depending on sort order.
    return max(0.0, min(100.0, (below + equal / 2.0) / len(dist) * 100.0))


def curve_clamp(value, lo, hi, dist):
    """
    Min-max between p5 and p95 instead of between the extremes.

    Keeps the absolute character of min-max — a machine's score depends on its
    own specification, not on its neighbours — while stopping one workstation
    from defining the top of the scale. Everything outside the window saturates,
    so the tails stop being distinguishable from each other.
    """
    if not dist:
        return curve_minmax(value, lo, hi, dist)
    p_lo, p_hi = percentile(dist, CLAMP_LO), percentile(dist, CLAMP_HI)
    if p_hi <= p_lo:
        return curve_minmax(value, lo, hi, dist)
    return max(0.0, min(100.0, (value - p_lo) / (p_hi - p_lo) * 100.0))


def curve_log(value, lo, hi, dist):
    """
    Min-max on log1p values. Compresses a long right tail without needing the
    distribution at all, so it stays absolute. Weak on ram_storage, where the
    values are a handful of discrete powers of two rather than a continuum.
    """
    if hi <= lo or value < 0:
        return 50.0
    a, b, v = math.log1p(lo), math.log1p(hi), math.log1p(value)
    if b <= a:
        return 50.0
    return max(0.0, min(100.0, (v - a) / (b - a) * 100.0))


CURVES = {
    "minmax": curve_minmax,
    "percentile": curve_percentile,
    "clamp": curve_clamp,
    "log": curve_log,
}

# Only two factors are actually broken under min-max: price (measured spread
# 34.1) and ram_storage (21.3). battery, weight, cpu and gpu already use most of
# their range, because their bounds are either physical — a laptop battery
# cannot exceed about 100Wh — or genuinely wide, as benchmark marks are.
#
# Applying percentile everywhere buys a uniform spread at the cost of making
# every score relative to the current catalog: listing or suspending one laptop
# moves all the others. ADR-0006 positions PickScore as a buy indicator, and the
# answer to "should I buy this one" should not change because a competitor was
# listed. Confining the relative curve to the two skewed factors keeps that
# property everywhere it can be kept, and price and capacity are the two factors
# where a comparative reading is natural anyway ("for this price", "how much
# storage is a lot these days").
#
# Keys are RANGE names, not breakdown factor names: ram_storage is a 0.6/0.4
# blend of ram_gb and storage_gb, so both have to be listed to change it.
MIXED_PLAN = {
    "price":      curve_percentile,
    "ram_gb":     curve_percentile,
    "storage_gb": curve_percentile,
    "weight_kg":  curve_minmax,
    "battery_wh": curve_minmax,
    "cpu_mark":   curve_minmax,
    "gpu_mark":   curve_minmax,
}

CURVES["mixed"] = MIXED_PLAN


# ---------------------------------------------------------------- plumbing


def build_distributions(session, ranges, cpu_bm, gpu_bm):
    """
    One sorted value list per factor, keyed by the factor's (min, max) pair.

    The engine's `_normalize(value, factor_range, inverse)` does not say which
    factor it is scoring, and every `_score_*` funnels through it. Keying on
    the range's (min, max) pair lets one patched function serve all six factors
    without touching engine code — the alternative, reimplementing the scoring here,
    would risk diverging from the engine, which is the exact class of bug the
    last two days were spent removing.
    """
    rows = session.execute(
        sa_select(
            Laptop.price_rm, Laptop.ram_gb, Laptop.ssd_gb,
            Laptop.weight_kg, Laptop.battery_wh,
            Laptop.processor_model, Laptop.gpu_model,
        ).where(Laptop.status == "active")
    ).all()

    dists = defaultdict(list)
    for price, ram, ssd, weight, batt, cpu_model, gpu_model in rows:
        if price and price > 0:
            dists["price"].append(float(price))
        for key, val in (("ram_gb", ram), ("storage_gb", ssd),
                         ("weight_kg", weight), ("battery_wh", batt)):
            if val is not None:
                dists[key].append(float(val))

        if cpu_model:
            r = resolve_benchmark(cpu_model, cpu_bm)
            if r["score"] is not None:
                dists["cpu_mark"].append(float(r["score"]))
        if gpu_model:
            r = (resolve_gpu_benchmark(gpu_model, cpu_model or "", gpu_bm)
                 if resolve_gpu_benchmark else resolve_benchmark(gpu_model, gpu_bm))
            if r["score"] is not None:
                dists["gpu_mark"].append(float(r["score"]))

    by_range = {}
    for factor, values in dists.items():
        if factor not in ranges:
            continue
        key = (float(ranges[factor]["min"]), float(ranges[factor]["max"]))
        if key in by_range:
            raise RuntimeError(
                "Two factors share the range {} — the (min,max) key is no longer "
                "unique and this script would score them with the wrong "
                "distribution.".format(key)
            )
        by_range[key] = (factor, sorted(values))
    return by_range


def patch_normalize(curve, by_range):
    """
    `curve` is either one function applied to every factor, or a dict mapping
    range name to function.

    The engine's `_normalize` takes the factor's whole range dict but still
    does not say which factor that is, so the (min, max) pair inside it
    identifies the factor — build_distributions asserts that pair is unique.

    The distribution handed to the curve is this script's own
    (build_distributions), never `factor_range["values"]`. That is the point:
    since ADR-0011 the engine computes percentile itself, so reusing its list
    would make the 'percentile' column a tautology instead of an independent
    check on the implementation.
    """
    def patched(value, factor_range, inverse=False):
        if value is None:
            return 50.0
        min_val = float((factor_range or {}).get("min", 0.0))
        max_val = float((factor_range or {}).get("max", 0.0))
        if max_val <= min_val:
            return 50.0
        factor, dist = by_range.get((min_val, max_val), (None, []))
        fn = curve.get(factor, curve_minmax) if isinstance(curve, dict) else curve
        score = fn(float(value), min_val, max_val, dist)
        return 100.0 - score if inverse else score

    pickscore_engine._normalize = patched


def score_all(session, ranges, cpu_bm, gpu_bm):
    """Every active laptop, every use case, under whatever curve is patched in."""
    laptops = session.exec(
        select(Laptop).where(Laptop.status == "active")
    ).all()
    brands = {b.id: b.name for b in session.exec(select(LaptopBrand)).all()}

    results = {uc: [] for uc in USE_CASE_PRIORITIES}
    raw_by_factor = defaultdict(list)

    for laptop in laptops:
        product = laptop_to_scorable(laptop, brands.get(laptop.brand_id, ""))
        for use_case, priorities in USE_CASE_PRIORITIES.items():
            resp = pickscore_engine.calculate_pick_score(
                product, None, ranges, cpu_bm, gpu_bm, priority_override=priorities
            )
            results[use_case].append((resp.score, laptop.model_code,
                                      laptop.price_rm, laptop.product_name,
                                      brands.get(laptop.brand_id, "")))
            if use_case == "gaming":  # raws are identical across presets
                for f in resp.breakdown:
                    raw_by_factor[f.factor].append(f.raw_score)

    for use_case in results:
        results[use_case].sort(key=lambda t: (-t[0], t[2] or 0))
    return results, raw_by_factor


def report_top(per_curve_scores, curves, top_n=5):
    """
    The real test: is the top of each use-case list the kind of machine that
    use case describes? A gaming list should be gaming laptops, an office list
    should be thin-and-light. One reference machine cannot show that.
    """
    for curve in curves:
        print(SEP)
        print("TOP {} PER USE CASE  —  {}".format(top_n, curve))
        print(SEP)
        for use_case in USE_CASE_PRIORITIES:
            print("\n  {}".format(use_case))
            for i, row in enumerate(per_curve_scores[curve][use_case][:top_n], 1):
                score, model_code, price, name, brand = row
                print("    {}. [{:>3}] {} {}".format(
                    i, score, brand, (name or "")[:54]))
                print("            RM{}".format(price))
        print()


# ---------------------------------------------------------------- reporting


def report_spread(per_curve_raws, curves):
    print(SEP)
    print("EFFECTIVE SPREAD PER FACTOR  (p10 .. p90 of raw scores, active only)")
    print(SEP)
    print("  {:<14}{}".format(
        "factor", "".join("{:<22}".format(c) for c in curves)))
    factors = ["price", "cpu", "gpu", "ram_storage", "portability", "battery"]
    for factor in factors:
        cells = []
        for c in curves:
            values = sorted(per_curve_raws[c].get(factor, []))
            if not values:
                cells.append("{:<22}".format("-"))
                continue
            lo, hi = percentile(values, 10), percentile(values, 90)
            cells.append("{:<22}".format(
                "{:5.1f}..{:5.1f} ({:4.1f})".format(lo, hi, hi - lo)))
        print("  {:<14}{}".format(factor, "".join(cells)))
    print("\n  Bracketed figure is the spread. Factors whose spreads differ")
    print("  widely are being weighted as if they were comparable.\n")


def report_reference(per_curve_scores, curves, model_code):
    print(SEP)
    print("USE-CASE SCORES FOR {}".format(model_code))
    print(SEP)
    print("  {:<16}{}".format(
        "use case", "".join("{:<14}".format(c) for c in curves)))
    for use_case in USE_CASE_PRIORITIES:
        cells = []
        for c in curves:
            hit = next((r[0] for r in per_curve_scores[c][use_case]
                        if r[1] == model_code), None)
            cells.append("{:<14}".format(hit if hit is not None else "-"))
        print("  {:<16}{}".format(use_case, "".join(cells)))

    print()
    for c in curves:
        scores = {uc: next((r[0] for r in per_curve_scores[c][uc]
                            if r[1] == model_code), None)
                  for uc in USE_CASE_PRIORITIES}
        if scores.get("gaming") is None or scores.get("office_study") is None:
            continue
        print("  {:<12} office_study - gaming = {}".format(
            c, scores["office_study"] - scores["gaming"]))
    print("\n  A negative or near-zero gap means the inversion closed.\n")


def report_overlap(per_curve_scores, curves, baseline="minmax", top_n=10):
    print(SEP)
    print("TOP-{} OVERLAP WITH '{}'".format(top_n, baseline))
    print(SEP)
    print("  {:<16}{}".format(
        "use case", "".join("{:<12}".format(c) for c in curves if c != baseline)))
    for use_case in USE_CASE_PRIORITIES:
        base = {r[1] for r in per_curve_scores[baseline][use_case][:top_n]}
        cells = []
        for c in curves:
            if c == baseline:
                continue
            other = {r[1] for r in per_curve_scores[c][use_case][:top_n]}
            cells.append("{:<12}".format("{}/{}".format(len(base & other), top_n)))
        print("  {:<16}{}".format(use_case, "".join(cells)))
    print("\n  A curve that leaves every list unchanged changes nothing a user")
    print("  sees, whatever it does to the numbers.\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-code", default=REFERENCE)
    parser.add_argument("--curves", nargs="+", default=list(CURVES),
                        choices=list(CURVES))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--show-top", type=int, default=5,
                        help="List this many machines per use case. 0 to skip.")
    args = parser.parse_args()

    with Session(db_engine) as session:
        ranges = get_laptop_ranges(session)
        cpu_bm = [(r.cpu_name, r.cpu_mark)
                  for r in session.exec(select(CPUBenchmark)).all()]
        gpu_bm = [(r.gpu_name, r.gpu_mark)
                  for r in session.exec(select(GPUBenchmark)).all()]

        by_range = build_distributions(session, ranges, cpu_bm, gpu_bm)

        print(SEP)
        print("RANGES IN USE")
        print(SEP)
        for key, value in ranges.items():
            _, values = by_range.get(
                (float(value["min"]), float(value["max"])), (None, []))
            # min/max only: since ADR-0011 the range dict also carries the
            # engine's own sorted value list, and printing it would bury the
            # report under seven lists of ~238 floats. `n` is this script's
            # independently built distribution; `engine_n` is the engine's, and
            # the two disagreeing is itself a finding.
            print("  {:<14}{{'min': {}, 'max': {}}}   n={}  engine_n={}".format(
                key, value["min"], value["max"], len(values),
                len(value.get("values", []))))
        print()

        per_curve_scores, per_curve_raws = {}, {}
        # Capture the real _normalize once. Reading it back inside the loop
        # would return the previous iteration's patch from the second curve on,
        # and the restore in `finally` would put a patched function back.
        original = pickscore_engine._normalize
        try:
            for name in args.curves:
                patch_normalize(CURVES[name], by_range)
                scores, raws = score_all(session, ranges, cpu_bm, gpu_bm)
                per_curve_scores[name], per_curve_raws[name] = scores, raws
        finally:
            pickscore_engine._normalize = original

        report_spread(per_curve_raws, args.curves)
        report_reference(per_curve_scores, args.curves, args.model_code)
        if "minmax" in args.curves and len(args.curves) > 1:
            report_overlap(per_curve_scores, args.curves, "minmax", args.top_n)
        if args.show_top:
            report_top(per_curve_scores, args.curves, args.show_top)


if __name__ == "__main__":
    main()