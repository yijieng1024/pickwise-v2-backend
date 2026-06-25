from typing import Optional
from app.laptops.laptop_models import Laptop
from app.users.models import LaptopUserPreference
from app.laptops.pickscore.benchmark_service import resolve_benchmark
from app.laptops.pickscore.schemas import FactorBreakdown, PickScoreResponse

# General mode base weights (N-i rule, 8 factors)
DEFAULT_PRIORITY: dict[str, float] = {
    "price":       8,
    "cpu":         7,
    "gpu":         6,
    "ram_storage": 5,
    "portability": 4,
    "battery":     3,
    "screen_size": 2,
    "brand":       1,
}

# Purpose → per-factor weight multipliers (capped at 1.3 per spec 3.4)
PURPOSE_MODIFIERS: dict[str, dict[str, float]] = {
    "Office/Study":             {"cpu": 1.1},
    "Programming/Development":  {"cpu": 1.2, "ram_storage": 1.2},
    "Gaming":                   {"gpu": 1.3, "cpu": 1.1},
    "Creative Work":            {"gpu": 1.3, "ram_storage": 1.2},
    "General Use":              {},
}

# Q5 portability intensity → Portability factor weight multiplier
PORTABILITY_MULTIPLIERS: dict[str, float] = {
    "Yes":     1.4,
    "Neutral": 1.0,
    "No":      0.5,
}

SCREEN_BUCKETS = ['13-14"', '15-16"', '17+"']
DECAY_K = 2


def _normalize(value: Optional[float], min_val: float, max_val: float, inverse: bool = False) -> float:
    if value is None:
        return 50.0
    if max_val <= min_val:
        return 50.0
    score = (value - min_val) / (max_val - min_val) * 100.0
    score = max(0.0, min(100.0, score))
    return 100.0 - score if inverse else score


def _laptop_screen_bucket(display_size_inch: float) -> int:
    if display_size_inch <= 14:
        return 0
    if display_size_inch <= 16:
        return 1
    return 2


def _user_screen_bucket(screen_size_pref: str) -> int:
    s = screen_size_pref.lower()
    if "13" in s or "14" in s:
        return 0
    if "15" in s or "16" in s:
        return 1
    return 2


def _score_price(laptop: Laptop, user_pref: Optional[LaptopUserPreference], ranges: dict, mode: str) -> float:
    if mode == "personalized" and user_pref and user_pref.budget:
        budget_max = float(user_pref.budget)
        price = laptop.price_rm
        if price <= budget_max:
            return 100.0
        over_ratio = (price - budget_max) / budget_max
        return max(0.0, 100.0 - DECAY_K * over_ratio * 100.0)
    return _normalize(laptop.price_rm, ranges["price_rm"]["min"], ranges["price_rm"]["max"], inverse=True)


def _score_cpu(laptop: Laptop, ranges: dict, cpu_benchmarks: list[tuple[str, int]]) -> tuple[float, dict]:
    result = resolve_benchmark(laptop.processor_model, cpu_benchmarks)
    if result["score"] is not None:
        score = _normalize(float(result["score"]), ranges["cpu_mark"]["min"], ranges["cpu_mark"]["max"])
    else:
        score = 50.0
    return score, result


def _score_gpu(
    laptop: Laptop,
    ranges: dict,
    gpu_benchmarks: list[tuple[str, int]],
    cpu_score: float,
    brand_name: str,
) -> tuple[float, bool]:
    result = resolve_benchmark(laptop.gpu_model, gpu_benchmarks)
    if result["score"] is not None:
        return _normalize(float(result["score"]), ranges["gpu_mark"]["min"], ranges["gpu_mark"]["max"]), False
    if brand_name.lower() == "apple":
        return cpu_score, True  # proxy: unified chip
    return 50.0, False


def _score_ram_storage(laptop: Laptop, ranges: dict) -> float:
    ram_score = _normalize(float(laptop.ram_gb), ranges["ram_gb"]["min"], ranges["ram_gb"]["max"])
    storage_score = _normalize(float(laptop.ssd_gb), ranges["ssd_gb"]["min"], ranges["ssd_gb"]["max"])
    if laptop.storage_type and "hdd" in laptop.storage_type.lower():
        storage_score = max(0.0, storage_score - 15.0)
    return ram_score * 0.6 + storage_score * 0.4


def _score_portability(laptop: Laptop, ranges: dict) -> float:
    return _normalize(laptop.weight_kg, ranges["weight_kg"]["min"], ranges["weight_kg"]["max"], inverse=True)


def _score_battery(laptop: Laptop, ranges: dict) -> float:
    return _normalize(laptop.battery_wh, ranges["battery_wh"]["min"], ranges["battery_wh"]["max"])


def _score_screen_size(laptop: Laptop, user_pref: Optional[LaptopUserPreference], mode: str) -> float:
    if mode != "personalized" or not user_pref or not user_pref.screen_size:
        return 50.0
    pref_list = user_pref.screen_size
    if not pref_list:
        return 50.0
    user_bucket = _user_screen_bucket(pref_list[0])
    laptop_bucket = _laptop_screen_bucket(laptop.display_size_inch)
    distance = abs(user_bucket - laptop_bucket)
    return max(0.0, 100.0 - distance * 40.0)


def _score_brand(laptop: Laptop, brand_name: str, user_pref: Optional[LaptopUserPreference], mode: str) -> float:
    if mode != "personalized" or not user_pref or not user_pref.brand_preferences:
        return 50.0
    prefs = [b.lower() for b in user_pref.brand_preferences]
    if not prefs or "no preference" in prefs:
        return 50.0
    return 100.0 if brand_name.lower() in prefs else 50.0


def _compute_weights(user_pref: Optional[LaptopUserPreference], mode: str) -> dict[str, float]:
    factors = ["price", "cpu", "gpu", "ram_storage", "portability", "battery", "screen_size", "brand"]

    if mode == "personalized" and user_pref:
        priorities = user_pref.priorities or {}
        base_weights = {f: float(priorities.get(f, 1)) for f in factors}

        # Purpose modifiers: take max across all selected purposes, cap at 1.3
        purpose_mods: dict[str, float] = {f: 1.0 for f in factors}
        for purpose in (user_pref.purpose or []):
            mods = PURPOSE_MODIFIERS.get(purpose, {})
            for factor, mult in mods.items():
                purpose_mods[factor] = min(1.3, max(purpose_mods[factor], mult))

        # Portability intensity modifier
        portability_str = user_pref.portability or "Neutral"
        portability_mult = 1.0
        for key, mult in PORTABILITY_MULTIPLIERS.items():
            if portability_str.startswith(key):
                portability_mult = mult
                break
    else:
        base_weights = dict(DEFAULT_PRIORITY)
        purpose_mods = {f: 1.0 for f in factors}
        portability_mult = 1.0

    final_weights: dict[str, float] = {}
    for f in factors:
        w = base_weights.get(f, 1.0) * purpose_mods.get(f, 1.0)
        if f == "portability":
            w *= portability_mult
        final_weights[f] = w

    total = sum(final_weights.values())
    return {f: w / total for f, w in final_weights.items()}


def calculate_pick_score(
    laptop: Laptop,
    brand_name: str,
    user_pref: Optional[LaptopUserPreference],
    ranges: dict,
    cpu_benchmarks: list[tuple[str, int]],
    gpu_benchmarks: list[tuple[str, int]],
) -> PickScoreResponse:
    mode = "personalized" if user_pref else "general"

    cpu_score, _cpu_result = _score_cpu(laptop, ranges, cpu_benchmarks)
    gpu_score, gpu_is_proxy = _score_gpu(laptop, ranges, gpu_benchmarks, cpu_score, brand_name)

    factor_scores = {
        "price":       _score_price(laptop, user_pref, ranges, mode),
        "cpu":         cpu_score,
        "gpu":         gpu_score,
        "ram_storage": _score_ram_storage(laptop, ranges),
        "portability": _score_portability(laptop, ranges),
        "battery":     _score_battery(laptop, ranges),
        "screen_size": _score_screen_size(laptop, user_pref, mode),
        "brand":       _score_brand(laptop, brand_name, user_pref, mode),
    }

    weights = _compute_weights(user_pref, mode)

    breakdown = []
    weighted_sum = 0.0
    for factor, raw_score in factor_scores.items():
        w = weights[factor]
        contribution = raw_score * w
        weighted_sum += contribution
        breakdown.append(FactorBreakdown(
            factor=factor,
            raw_score=round(raw_score, 2),
            weight=round(w, 4),
            contribution=round(contribution, 2),
        ))

    final_score = int(max(0, min(100, round(weighted_sum))))

    return PickScoreResponse(
        laptop_id=laptop.id,
        score=final_score,
        mode=mode,
        breakdown=breakdown,
        flags={"gpu_score_is_proxy": gpu_is_proxy},
    )
