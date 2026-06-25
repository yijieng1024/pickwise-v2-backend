from sqlalchemy import func, select
from sqlmodel import Session

from app.laptops.laptop_models import Laptop
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.pickscore.schemas import ScorableProduct
from app.pickscore.ranges_cache import get_cached_ranges, set_cached_ranges

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
        select(
            func.min(Laptop.price_rm),  func.max(Laptop.price_rm),
            func.min(Laptop.ram_gb),    func.max(Laptop.ram_gb),
            func.min(Laptop.ssd_gb),    func.max(Laptop.ssd_gb),
            func.min(Laptop.weight_kg), func.max(Laptop.weight_kg),
            func.min(Laptop.battery_wh),func.max(Laptop.battery_wh),
        )
    ).one()

    cpu_row = session.execute(
        select(func.min(CPUBenchmark.cpu_mark), func.max(CPUBenchmark.cpu_mark))
    ).one()

    gpu_row = session.execute(
        select(func.min(GPUBenchmark.gpu_mark), func.max(GPUBenchmark.gpu_mark))
    ).one()

    ranges = {
        "price":      {"min": laptop_row[0] or 0.0, "max": laptop_row[1] or 0.0},
        "ram_gb":     {"min": laptop_row[2] or 0,   "max": laptop_row[3] or 0},
        "storage_gb": {"min": laptop_row[4] or 0,   "max": laptop_row[5] or 0},
        "weight_kg":  {"min": laptop_row[6] or 0.0, "max": laptop_row[7] or 0.0},
        "battery_wh": {"min": laptop_row[8] or 0.0, "max": laptop_row[9] or 0.0},
        "cpu_mark":   {"min": cpu_row[0] or 0,      "max": cpu_row[1] or 0},
        "gpu_mark":   {"min": gpu_row[0] or 0,      "max": gpu_row[1] or 0},
    }
    set_cached_ranges(_CACHE_KEY, ranges)
    return ranges
