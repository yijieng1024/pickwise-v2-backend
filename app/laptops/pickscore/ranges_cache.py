import time
from sqlalchemy import func, select
from sqlmodel import Session

from app.laptops.laptop_models import Laptop
from app.benchmark.model import CPUBenchmark, GPUBenchmark

_cache: dict = {}
_cache_time: float = 0.0
CACHE_TTL = 300  # 5 minutes


def get_statistical_ranges(session: Session) -> dict:
    global _cache, _cache_time
    now = time.time()
    if _cache and (now - _cache_time) < CACHE_TTL:
        return _cache

    laptop_row = session.execute(
        select(
            func.min(Laptop.price_rm),
            func.max(Laptop.price_rm),
            func.min(Laptop.ram_gb),
            func.max(Laptop.ram_gb),
            func.min(Laptop.ssd_gb),
            func.max(Laptop.ssd_gb),
            func.min(Laptop.weight_kg),
            func.max(Laptop.weight_kg),
            func.min(Laptop.battery_wh),
            func.max(Laptop.battery_wh),
        )
    ).one()

    cpu_row = session.execute(
        select(func.min(CPUBenchmark.cpu_mark), func.max(CPUBenchmark.cpu_mark))
    ).one()

    gpu_row = session.execute(
        select(func.min(GPUBenchmark.gpu_mark), func.max(GPUBenchmark.gpu_mark))
    ).one()

    _cache = {
        "price_rm":   {"min": laptop_row[0] or 0.0, "max": laptop_row[1] or 0.0},
        "ram_gb":     {"min": laptop_row[2] or 0,   "max": laptop_row[3] or 0},
        "ssd_gb":     {"min": laptop_row[4] or 0,   "max": laptop_row[5] or 0},
        "weight_kg":  {"min": laptop_row[6] or 0.0, "max": laptop_row[7] or 0.0},
        "battery_wh": {"min": laptop_row[8] or 0.0, "max": laptop_row[9] or 0.0},
        "cpu_mark":   {"min": cpu_row[0] or 0,      "max": cpu_row[1] or 0},
        "gpu_mark":   {"min": gpu_row[0] or 0,      "max": gpu_row[1] or 0},
    }
    _cache_time = now
    return _cache
