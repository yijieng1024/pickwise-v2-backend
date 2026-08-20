from sqlalchemy import func, select as sa_select
from sqlmodel import Session, select

from app.laptops.laptop_models import Laptop, LaptopStatus
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.pickscore.schemas import ScorableProduct
from app.pickscore.ranges_cache import get_cached_ranges, set_cached_ranges
from app.pickscore.benchmark_service import resolve_benchmark, resolve_gpu_benchmark

_CACHE_KEY = "laptop_ranges"


def laptop_to_scorable(laptop: Laptop, brand_name: str) -> ScorableProduct:
    return ScorableProduct(
        product_id=laptop.id,
        brand_name=brand_name,
        price=laptop.price_rm,
        cpu_model=laptop.processor_model,
        gpu_model=laptop.gpu_model,
        ram_gb=laptop.ram_gb,
        storage_gb=laptop.ssd_gb,
        storage_type=laptop.storage_type,
        weight_kg=laptop.weight_kg,
        battery_wh=laptop.battery_wh,
        display_size_inch=laptop.display_size_inch,
    )


def _distribution(values: list[float]) -> dict:
    """
    One factor's range: the sorted value list the engine ranks against, plus
    the min and max that list implies.

    `values` is the whole distribution because _normalize scores by percentile
    rank (ADR-0011) — min-max made each factor's scale a function of exactly
    two rows, and for price, capacity and weight those two rows are outliers.
    `min`/`max` are kept because _score_price's personalized branch and the
    diagnostic scripts still read them, and because they are the min-max
    fallback _normalize uses if a distribution is ever missing.

    One entry per laptop, not per distinct value: a configuration that eight
    machines share should weigh eight times as much on the curve as one that
    is unique.
    """
    if not values:
        return {"min": 0.0, "max": 0.0, "values": []}
    ordered = sorted(values)
    return {"min": ordered[0], "max": ordered[-1], "values": ordered}


def get_laptop_ranges(session: Session, force_refresh: bool = False) -> dict:
    if not force_refresh:
        cached = get_cached_ranges(_CACHE_KEY)
        if cached:
            return cached

    # One row per active laptop, and every distribution is built from it, so
    # the min/max can never disagree with the values list they summarize.
    catalog_rows = session.execute(
        sa_select(
            Laptop.price_rm, Laptop.ram_gb, Laptop.ssd_gb,
            Laptop.weight_kg, Laptop.battery_wh,
            Laptop.processor_model, Laptop.gpu_model,
        ).where(Laptop.status == LaptopStatus.ACTIVE.value)
    ).all()

    # Fetch full benchmark lists once
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]

    # Resolve benchmark scores for laptops actually in the catalog so the
    # normalization range reflects laptop-class hardware, not desktop/server
    # CPUs. Memoized on the model string / (cpu, gpu) pair so the per-laptop
    # distribution costs the same number of fuzzy matches the old per-distinct
    # -model range did.
    _cpu_marks: dict[str, object] = {}
    _gpu_marks: dict[tuple, object] = {}

    def cpu_mark(model: str):
        if model not in _cpu_marks:
            _cpu_marks[model] = resolve_benchmark(model, cpu_benchmarks)["score"]
        return _cpu_marks[model]

    def gpu_mark(cpu: str, gpu: str):
        # (cpu, gpu) pairs, not GPU strings alone: an anchorless GPU name is
        # only resolvable through the CPU it is fused to, so the range layer
        # has to ask the same question the engine asks. Sharing
        # resolve_gpu_benchmark is also what keeps an unresolvable GPU out of
        # the denominator without a brand special-case.
        key = (cpu, gpu)
        if key not in _gpu_marks:
            _gpu_marks[key] = resolve_gpu_benchmark(gpu, cpu, gpu_benchmarks)["score"]
        return _gpu_marks[key]

    prices: list[float] = []
    rams: list[float] = []
    storages: list[float] = []
    weights: list[float] = []
    batteries: list[float] = []
    cpu_scores: list[float] = []
    gpu_scores: list[float] = []

    for price_rm, ram_gb, ssd_gb, weight_kg, battery_wh, cpu_model, gpu_model in catalog_rows:
        # price_rm = 0 means "price unknown" (scored neutral 50 by the engine)
        # — it must not drag the floor down or every real price normalizes
        # against a fictional free laptop.
        if price_rm is not None and price_rm > 0:
            prices.append(float(price_rm))
        for bucket, value in ((rams, ram_gb), (storages, ssd_gb),
                              (weights, weight_kg), (batteries, battery_wh)):
            if value is not None:
                bucket.append(float(value))

        if cpu_model and (mark := cpu_mark(cpu_model)) is not None:
            cpu_scores.append(float(mark))
        if gpu_model and (mark := gpu_mark(cpu_model or "", gpu_model)) is not None:
            gpu_scores.append(float(mark))

    # Fall back to the global benchmark table if no catalog laptop resolved.
    # There is no per-laptop distribution to fall back to in that case, so the
    # empty `values` list sends _normalize down its min-max path.
    if not cpu_scores:
        fallback = session.execute(sa_select(func.min(CPUBenchmark.cpu_mark), func.max(CPUBenchmark.cpu_mark))).one()
        cpu_range = {"min": float(fallback[0] or 0), "max": float(fallback[1] or 0), "values": []}
    else:
        cpu_range = _distribution(cpu_scores)

    if not gpu_scores:
        fallback = session.execute(sa_select(func.min(GPUBenchmark.gpu_mark), func.max(GPUBenchmark.gpu_mark))).one()
        gpu_range = {"min": float(fallback[0] or 0), "max": float(fallback[1] or 0), "values": []}
    else:
        gpu_range = _distribution(gpu_scores)

    ranges = {
        "price":      _distribution(prices),
        "ram_gb":     _distribution(rams),
        "storage_gb": _distribution(storages),
        "weight_kg":  _distribution(weights),
        "battery_wh": _distribution(batteries),
        "cpu_mark":   cpu_range,
        "gpu_mark":   gpu_range,
    }
    set_cached_ranges(_CACHE_KEY, ranges)
    return ranges
