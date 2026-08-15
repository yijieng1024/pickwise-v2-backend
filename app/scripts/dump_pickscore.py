"""
Dump the PickScore factor breakdown for one laptop, plus the catalog-wide
normalization ranges and the zero-value counts that set them.

Answers the open questions in ADR-0006:
  1. Which columns have their normalization floor set by missing data?
     `price_rm` is guarded by a `> 0` filter in pickscore_adapter.py:39; the
     other normalized columns are not.
  2. Does the GPU/CPU benchmark lookup resolve, or does a modern part score as
     if it had no benchmark at all?
  3. Do the five use-case presets differ in their raw scores, or only in the
     weights applied to them? Those need different fixes — wrong weights are a
     tuning problem, depressed raw scores are a normalization problem.

Usage:
    python dump_pickscore.py "TUF Gaming F16"
    python dump_pickscore.py "TUF Gaming F16" --model-code FX608JMR
    python dump_pickscore.py "TUF Gaming F16" --use-cases
"""
import argparse
import sys

from sqlalchemy import func, select as sa_select
from sqlmodel import Session, select

from app.database import engine
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.brand_model import LaptopBrand

# Mapper registration for a standalone script: Laptop -> LaptopCustomization ->
# Category is a chain of string-name relationships whose targets are only
# imported under TYPE_CHECKING. main.py resolves them by importing every
# router; here each link has to be imported by hand or configure_mappers()
# fails. The chain grew when category_id was added to customizations.
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.laptop_models import Laptop
from app.laptops.pickscore_adapter import get_laptop_ranges, laptop_to_scorable
from app.pickscore.engine import calculate_pick_score

# Columns feeding min-max normalization. Only price_rm is guarded against zeros
# upstream, so the rest can have their floor set by missing data.
_NORMALIZED_COLUMNS = ("price_rm", "ram_gb", "ssd_gb", "weight_kg", "battery_wh")

SEP = "=" * 68


def _rule(title):
    print(SEP)
    print(title)
    print(SEP)


def _load_use_case_priorities():
    """
    The five use-case scores in the UI (General / Office & Study / Programming
    / Gaming / Creative) are not produced by calculate_pick_score(user_pref=
    None) — that yields one general-mode score.

    Adjust these candidates to wherever the presets actually live. If none
    resolve, the script says so rather than silently printing the same number
    five times.
    """
    candidates = (
        ("app.pickscore.engine", "USE_CASE_PRIORITIES"),
        ("app.pickscore.presets", "USE_CASE_PRIORITIES"),
        ("app.pickscore.constants", "USE_CASE_PRIORITIES"),
    )
    for module, attr in candidates:
        try:
            mod = __import__(module, fromlist=[attr])
            return getattr(mod, attr), module + "." + attr
        except (ImportError, AttributeError):
            continue
    return None, None


def dump_ranges(session):
    ranges = get_laptop_ranges(session)

    _rule("CATALOG NORMALIZATION RANGES")
    if isinstance(ranges, dict):
        for key, value in ranges.items():
            print("  {:<14} {}".format(key, value))
    else:
        print("  {}".format(ranges))
    print()

    print("  Rows at 0 per normalized column:")
    for col in _NORMALIZED_COLUMNS:
        attr = getattr(Laptop, col, None)
        if attr is None:
            print("    {:<12} (column not found)".format(col))
            continue
        count = session.execute(
            sa_select(func.count()).select_from(Laptop).where(attr == 0)
        ).scalar_one()
        flag = "   <- guarded by the > 0 filter" if col == "price_rm" else ""
        print("    {:<12} {:>4}{}".format(col, count, flag))
    print()
    return ranges


def pick_laptop(session, name, model_code):
    """
    Select one variant deterministically.

    The earlier version used .first() with no ORDER BY. Families in this
    catalog hold up to 14 configurations, so it returned whichever row
    Postgres happened to hand back — different GPU, different price, different
    score on every run. The variant ambiguity this script exists to
    investigate was also corrupting the script's own output.
    """
    stmt = (
        select(Laptop, LaptopBrand.name)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .where(Laptop.product_name.ilike("%{}%".format(name)))
        .order_by(Laptop.model_code)
    )
    if model_code:
        stmt = stmt.where(Laptop.model_code == model_code)

    rows = session.exec(stmt).all()

    if not rows:
        suffix = " / {}".format(model_code) if model_code else ""
        print("No laptop matching '{}'{}".format(name, suffix))
        sys.exit(1)

    if len(rows) > 1:
        _rule("{} VARIANTS MATCH '{}'".format(len(rows), name))
        for laptop, _brand in rows:
            print("  {:<16} RM{:<9} {:<32} {}GB / {}GB".format(
                laptop.model_code or "-",
                laptop.price_rm,
                (laptop.gpu_model or "-")[:32],
                laptop.ram_gb,
                laptop.ssd_gb,
            ))
        print("\n  Dumping the first by model_code. "
              "Pass --model-code to pick a specific one.\n")

    return rows[0]


def dump_benchmark_lookup(laptop, gpu_benchmarks, cpu_benchmarks):
    """
    Anchor the diagnostic on the model number, not on generic words.

    Matching any token from "geforce rtx 5060 laptop gpu" returns roughly half
    the benchmark table — every row containing "rtx" or "gpu" — which neither
    confirms nor rules out a resolution failure. Requiring every model-number
    token narrows it to rows that could actually be the right part.

    This is NOT the engine's own matcher (RapidFuzz with a confidence score).
    It only answers "does a plausible row exist at all"; the engine's actual
    resolution shows up in the breakdown below.
    """
    _rule("BENCHMARK LOOKUP")

    for label, model, table in (
        ("GPU", laptop.gpu_model, gpu_benchmarks),
        ("CPU", laptop.processor_model, cpu_benchmarks),
    ):
        needle = (model or "").lower().replace("-", " ")
        model_tokens = [
            t for t in needle.split()
            if any(c.isdigit() for c in t) and len(t) >= 3
        ]
        if model_tokens:
            hits = [
                (n, m) for n, m in table
                if all(t in n.lower().replace("-", " ") for t in model_tokens)
            ]
        else:
            hits = []

        print("  {}: {}".format(label, model))
        print("    model tokens  : {}".format(model_tokens or "(none - cannot anchor)"))
        print("    matching rows : {}".format(len(hits)))
        for n, m in hits[:5]:
            print("      {}  ->  {}".format(n, m))
        marks = [m for _, m in table if m]
        if marks:
            print("    table range   : {} .. {}".format(min(marks), max(marks)))
        print()


def dump_breakdown(product, ranges, cpu_benchmarks, gpu_benchmarks,
                   user_pref=None, title="PICK SCORE (general mode)"):
    resp = calculate_pick_score(product, user_pref, ranges, cpu_benchmarks, gpu_benchmarks)

    _rule("{}: {}".format(title, resp.score))
    print("  {:<22}{:>12}{:>15}".format("factor", "raw_score", "contribution"))
    for f in resp.breakdown:
        print("  {:<22}{:>12.1f}{:>15.2f}".format(f.factor, f.raw_score, f.contribution))
    print()

    return {f.factor: f.raw_score for f in resp.breakdown}


def dump_use_cases(product, ranges, cpu_benchmarks, gpu_benchmarks):
    presets, origin = _load_use_case_priorities()

    if presets is None:
        _rule("USE-CASE PRESETS: NOT FOUND")
        print("  Could not locate USE_CASE_PRIORITIES. Find it first:")
        print("    grep -rn 'USE_CASE\\|use_case' app/pickscore/ app/recommendation/")
        print("  Until then this section cannot reproduce the five UI scores —")
        print("  calling calculate_pick_score(user_pref=None) five times would")
        print("  print the same number five times.\n")
        return

    _rule("USE-CASE PRESETS (from {})".format(origin))
    raw_by_case = {}

    for case_name, priorities in presets.items():
        print("  {}: {}".format(case_name, priorities))
        # Adapt this call if the engine consumes presets in a different shape
        # than a preference object. The comparison below is the actual point.
        raw_by_case[case_name] = dump_breakdown(
            product, ranges, cpu_benchmarks, gpu_benchmarks,
            user_pref=priorities,
            title="PICK SCORE ({})".format(case_name),
        )

    # The question this whole section exists to answer.
    _rule("DO RAW SCORES VARY ACROSS PRESETS?")
    factors = sorted({f for d in raw_by_case.values() for f in d})
    identical = True
    for factor in factors:
        values = {case: d.get(factor) for case, d in raw_by_case.items()}
        distinct = {v for v in values.values() if v is not None}
        if len(distinct) > 1:
            identical = False
            print("  {:<22} varies: {}".format(factor, values))

    if identical:
        print("  Raw scores are identical across all presets.")
        print("  => The inversion comes from the preset WEIGHTS. Tuning them is")
        print("     the right fix.")
    else:
        print("\n  => Raw scores differ across presets, so the weights are not")
        print("     the only variable. Fix normalization before tuning weights.")
    print()


def main():
    parser = argparse.ArgumentParser(description="Dump a PickScore breakdown.")
    parser.add_argument("name", nargs="?", default="TUF Gaming F16")
    parser.add_argument("--model-code", default=None,
                        help="Disambiguate when a family has several variants.")
    parser.add_argument("--use-cases", action="store_true",
                        help="Also dump the breakdown under each use-case preset.")
    args = parser.parse_args()

    with Session(engine) as session:
        ranges = dump_ranges(session)
        laptop, brand_name = pick_laptop(session, args.name, args.model_code)

        _rule("{} {}".format(brand_name, laptop.product_name))
        print("  model_code : {}".format(laptop.model_code))
        print("  price_rm   : {}".format(laptop.price_rm))
        print("  processor  : {}".format(laptop.processor_model))
        print("  gpu        : {}".format(laptop.gpu_model))
        print("  ram / ssd  : {}GB / {}GB".format(laptop.ram_gb, laptop.ssd_gb))
        print("  weight     : {}kg".format(laptop.weight_kg))
        print("  battery    : {}Wh".format(laptop.battery_wh))
        print()

        cpu_benchmarks = [(r.cpu_name, r.cpu_mark)
                          for r in session.exec(select(CPUBenchmark)).all()]
        gpu_benchmarks = [(r.gpu_name, r.gpu_mark)
                          for r in session.exec(select(GPUBenchmark)).all()]

        dump_benchmark_lookup(laptop, gpu_benchmarks, cpu_benchmarks)

        product = laptop_to_scorable(laptop, brand_name)
        dump_breakdown(product, ranges, cpu_benchmarks, gpu_benchmarks)

        if args.use_cases:
            dump_use_cases(product, ranges, cpu_benchmarks, gpu_benchmarks)


if __name__ == "__main__":
    main()