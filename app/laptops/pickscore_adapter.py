from sqlalchemy import func, select as sa_select
from sqlmodel import Session, select

from app.laptops.laptop_models import Laptop
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.pickscore.schemas import ScorableProduct
from app.pickscore.ranges_cache import get_cached_ranges, set_cached_ranges
from app.pickscore.benchmark_service import resolve_benchmark

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


def get_laptop_ranges(session: Session) -> dict:
    cached = get_cached_ranges(_CACHE_KEY)
    if cached:
        return cached

    laptop_row = session.execute(
        sa_select(
            # price_rm = 0 means "price unknown" (scored neutral 50 by the
            # engine) — it must not drag the min down or every real price
            # normalizes against a fictional free laptop.
            func.min(Laptop.price_rm).filter(Laptop.price_rm > 0),
            func.max(Laptop.price_rm),
            func.min(Laptop.ram_gb),    func.max(Laptop.ram_gb),
            func.min(Laptop.ssd_gb),    func.max(Laptop.ssd_gb),
            func.min(Laptop.weight_kg), func.max(Laptop.weight_kg),
            func.min(Laptop.battery_wh),func.max(Laptop.battery_wh),
        )
    ).one()

    # Fetch full benchmark lists once
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]

    # Resolve benchmark scores for laptops actually in the catalog so the
    # normalization range reflects laptop-class hardware, not desktop/server CPUs
    catalog_rows = session.execute(sa_select(Laptop.processor_model, Laptop.gpu_model)).all()
    cpu_models = {row[0] for row in catalog_rows if row[0]}
    gpu_models = {row[1] for row in catalog_rows if row[1]}

    cpu_scores = [
        r["score"] for model in cpu_models
        if (r := resolve_benchmark(model, cpu_benchmarks))["score"] is not None
    ]
    gpu_scores = [
        r["score"] for model in gpu_models
        if (r := resolve_benchmark(model, gpu_benchmarks))["score"] is not None
    ]

    # Fall back to global table range if no catalog laptops matched
    if cpu_scores:
        cpu_min, cpu_max = min(cpu_scores), max(cpu_scores)
    else:
        fallback = session.execute(sa_select(func.min(CPUBenchmark.cpu_mark), func.max(CPUBenchmark.cpu_mark))).one()
        cpu_min, cpu_max = fallback[0] or 0, fallback[1] or 0

    if gpu_scores:
        gpu_min, gpu_max = min(gpu_scores), max(gpu_scores)
    else:
        fallback = session.execute(sa_select(func.min(GPUBenchmark.gpu_mark), func.max(GPUBenchmark.gpu_mark))).one()
        gpu_min, gpu_max = fallback[0] or 0, fallback[1] or 0

    ranges = {
        "price":      {"min": laptop_row[0] or 0.0, "max": laptop_row[1] or 0.0},
        "ram_gb":     {"min": laptop_row[2] or 0,   "max": laptop_row[3] or 0},
        "storage_gb": {"min": laptop_row[4] or 0,   "max": laptop_row[5] or 0},
        "weight_kg":  {"min": laptop_row[6] or 0.0, "max": laptop_row[7] or 0.0},
        "battery_wh": {"min": laptop_row[8] or 0.0, "max": laptop_row[9] or 0.0},
        "cpu_mark":   {"min": cpu_min, "max": cpu_max},
        "gpu_mark":   {"min": gpu_min, "max": gpu_max},
    }
    set_cached_ranges(_CACHE_KEY, ranges)
    return ranges
