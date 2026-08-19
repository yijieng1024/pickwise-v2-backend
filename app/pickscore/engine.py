from typing import Optional
from app.users.models import LaptopUserPreference
from app.pickscore.benchmark_service import resolve_benchmark, resolve_gpu_benchmark
from app.pickscore.schemas import ScorableProduct, FactorBreakdown, PickScoreResponse

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

# Purpose → per-factor weight multipliers, boost-only and capped at 1.3 per
# spec 3.4 (values must stay >= 1.0 — _compute_weights combines multiple
# selected purposes via max(), which would silently swallow anything lower).
# Tiers mirror each factor's relative rank inside USE_CASE_PRIORITIES (see
# app/laptops/pickscore_general.py): 1.3 = the defining factor(s) for that
# purpose, 1.2 = a close second tier, 1.1 = a minor but still above-baseline
# factor. Office/Study and General Use lead with price because PickScore's
# price/value factor is the first priority for everyday, non-performance
# use; boosting the other relevant factors already dilutes gpu's share
# without needing an explicit suppression entry.
PURPOSE_MODIFIERS: dict[str, dict[str, float]] = {
    "Office/Study": {
        "price": 1.3, "battery": 1.2, "portability": 1.2, "cpu": 1.1,
    },
    "Programming/Development": {
        "cpu": 1.3, "ram_storage": 1.3, "price": 1.1, "battery": 1.1,
    },
    "Gaming": {
        "gpu": 1.3, "cpu": 1.2, "ram_storage": 1.1,
    },
    "Creative Work": {
        "gpu": 1.3, "cpu": 1.2, "ram_storage": 1.2, "screen_size": 1.1,
    },
    "General Use": {
        "price": 1.3, "cpu": 1.2, "ram_storage": 1.2, "portability": 1.1, "battery": 1.1,
    },
}

# Q5 portability intensity → Portability factor weight multiplier
PORTABILITY_MULTIPLIERS: dict[str, float] = {
    "Yes":     1.4,
    "Neutral": 1.0,
    "No":      0.5,
}

DECAY_K = 2


def _normalize(value: Optional[float], min_val: float, max_val: float, inverse: bool = False) -> float:
    if value is None:
        return 50.0
    if max_val <= min_val:
        return 50.0
    score = (value - min_val) / (max_val - min_val) * 100.0
    score = max(0.0, min(100.0, score))
    return 100.0 - score if inverse else score


def _product_screen_bucket(display_size_inch: float) -> int:
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


def _score_price(
    product: ScorableProduct,
    user_pref: Optional[LaptopUserPreference],
    ranges: dict,
    mode: str,
) -> tuple[float, Optional[str]]:
    if product.price == 0.0:
        return 50.0, "Price unavailable — factor skipped, scored as neutral (50)"
    if mode == "personalized" and user_pref and user_pref.budget and user_pref.budget.get("max") is not None:
        budget_max = float(user_pref.budget["max"])
        if product.price <= budget_max:
            return 100.0, None
        over_ratio = (product.price - budget_max) / budget_max
        return max(0.0, 100.0 - DECAY_K * over_ratio * 100.0), None
    return _normalize(product.price, ranges["price"]["min"], ranges["price"]["max"], inverse=True), None


def _score_cpu(product: ScorableProduct, ranges: dict, cpu_benchmarks: list[tuple[str, int]]) -> tuple[float, dict]:
    result = resolve_benchmark(product.cpu_model, cpu_benchmarks)
    score = _normalize(float(result["score"]), ranges["cpu_mark"]["min"], ranges["cpu_mark"]["max"]) if result["score"] is not None else 50.0
    return score, result


# Why a GPU score is what it is, in the words a laptop buyer would need. Keyed
# on resolve_gpu_benchmark's `resolution`, so the reason travels with the number
# instead of being re-derived here -- re-deriving it would mean a second copy of
# the key normalization, which is how the last four benchmark defects happened.
_GPU_NOTES: dict[str, Optional[str]] = {
    "direct": None,
    "laptop_variant": None,
    "apple_equivalent": (
        "Apple Silicon GPU — PassMark has no ARM entries, so this is the "
        "closest non-Apple laptop GPU on a benchmark both have run"
    ),
    "integrated": (
        "Integrated graphics — scored via the CPU's known iGPU "
        "(the GPU name carries no model number)"
    ),
    "unresolved": (
        "GPU could not be identified — scored as neutral (50) and flagged "
        "as unverified"
    ),
}


def _score_gpu(
    product: ScorableProduct,
    ranges: dict,
    gpu_benchmarks: list[tuple[str, int]],
) -> tuple[float, bool, Optional[str]]:
    """
    Returns (score, is_proxy, note).

    The Apple short-circuit that used to sit at the top of this function
    returned cpu_score directly as the GPU score. Under percentile
    normalization a top-end Apple CPU sits near the 95th percentile, so the
    Gaming preset counted the same number twice — cpu (weight 8) and gpu
    (weight 10), half the preset — which put the MacBook Pro M5 Max above
    every ROG Strix SCAR. Apple GPUs now resolve through
    _APPLE_GPU_EQUIVALENT like any other part (ADR-0010, ADR-0011).

    `is_proxy` still covers three situations — an Apple cross-architecture
    equivalent, an integrated GPU resolved through its CPU, and a GPU that
    did not resolve at all. They share the flag because
    get_ranking_for_use_case demotes it in the gaming sort and all three
    deserve that demotion; the note is what keeps them distinguishable to a
    reader. Splitting them properly means a `gpu_resolution` field on
    PickScoreResponse.flags and a change to every consumer of it.
    """
    result = resolve_gpu_benchmark(product.gpu_model, product.cpu_model, gpu_benchmarks)
    note = _GPU_NOTES.get(result.get("resolution"))

    if result["score"] is not None:
        score = _normalize(
            float(result["score"]),
            ranges["gpu_mark"]["min"],
            ranges["gpu_mark"]["max"],
        )
        return score, result["is_proxy"], note

    # was `return 50.0, False` — a neutral 50 outranks a real RTX 3050 (30.4)
    # and RTX 4050 (48.0) on the current gpu_mark range, so an unresolved GPU
    # must at least be flagged.
    return 50.0, True, note


def _score_ram_storage(product: ScorableProduct, ranges: dict) -> float:
    ram_score = _normalize(float(product.ram_gb), ranges["ram_gb"]["min"], ranges["ram_gb"]["max"])
    storage_score = _normalize(float(product.storage_gb), ranges["storage_gb"]["min"], ranges["storage_gb"]["max"])
    if product.storage_type and "hdd" in product.storage_type.lower():
        storage_score = max(0.0, storage_score - 15.0)
    return ram_score * 0.6 + storage_score * 0.4


def _score_portability(product: ScorableProduct, ranges: dict) -> float:
    return _normalize(product.weight_kg, ranges["weight_kg"]["min"], ranges["weight_kg"]["max"], inverse=True)


def _score_battery(product: ScorableProduct, ranges: dict) -> float:
    return _normalize(product.battery_wh, ranges["battery_wh"]["min"], ranges["battery_wh"]["max"])


def _score_screen_size(product: ScorableProduct, user_pref: Optional[LaptopUserPreference], mode: str) -> float:
    if mode != "personalized" or not user_pref or not user_pref.screen_size:
        return 50.0
    user_bucket = _user_screen_bucket(user_pref.screen_size[0])
    product_bucket = _product_screen_bucket(product.display_size_inch)
    distance = abs(user_bucket - product_bucket)
    return max(0.0, 100.0 - distance * 40.0)


def _score_brand(product: ScorableProduct, user_pref: Optional[LaptopUserPreference], mode: str) -> float:
    if mode != "personalized" or not user_pref or not user_pref.brand_preferences:
        return 50.0
    prefs = [b.lower() for b in user_pref.brand_preferences]
    if not prefs or "no preference" in prefs:
        return 50.0
    return 100.0 if product.brand_name.lower() in prefs else 50.0


def _compute_weights(
    user_pref: Optional[LaptopUserPreference],
    mode: str,
    priority_override: Optional[dict[str, float]] = None,
) -> dict[str, float]:
    factors = ["price", "cpu", "gpu", "ram_storage", "portability", "battery", "screen_size", "brand"]

    if mode == "personalized" and user_pref:
        priorities = user_pref.priorities or {}
        base_weights = {f: float(priorities.get(f, 1)) for f in factors}

        purpose_mods: dict[str, float] = {f: 1.0 for f in factors}
        for purpose in (user_pref.purpose or []):
            for factor, mult in PURPOSE_MODIFIERS.get(purpose, {}).items():
                purpose_mods[factor] = min(1.3, max(purpose_mods[factor], mult))

        portability_str = user_pref.portability or "Neutral"
        portability_mult = next(
            (mult for key, mult in PORTABILITY_MULTIPLIERS.items() if portability_str.startswith(key)),
            1.0,
        )
    else:
        # General mode: callers may swap the balanced N-i profile for a
        # use-case profile (e.g. Gaming weighs GPU highest) while keeping
        # general-mode factor scoring (inverse min-max price, neutral 50
        # for screen size / brand).
        base_weights = dict(priority_override or DEFAULT_PRIORITY)
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
    product: ScorableProduct,
    user_pref: Optional[LaptopUserPreference],
    ranges: dict,
    cpu_benchmarks: list[tuple[str, int]],
    gpu_benchmarks: list[tuple[str, int]],
    priority_override: Optional[dict[str, float]] = None,
) -> PickScoreResponse:
    """priority_override only applies in general mode (user_pref is None) —
    a real user's priorities always win over a use-case profile."""
    mode = "personalized" if user_pref else "general"

    cpu_score, _ = _score_cpu(product, ranges, cpu_benchmarks)
    gpu_score, gpu_is_proxy, gpu_note = _score_gpu(product, ranges, gpu_benchmarks)
    price_score, price_note = _score_price(product, user_pref, ranges, mode)

    factor_scores = {
        "price":       price_score,
        "cpu":         cpu_score,
        "gpu":         gpu_score,
        "ram_storage": _score_ram_storage(product, ranges),
        "portability": _score_portability(product, ranges),
        "battery":     _score_battery(product, ranges),
        "screen_size": _score_screen_size(product, user_pref, mode),
        "brand":       _score_brand(product, user_pref, mode),
    }

    factor_notes: dict[str, Optional[str]] = {
        "price": price_note,
        "gpu":   gpu_note,
    }

    weights = _compute_weights(user_pref, mode, priority_override)

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
            note=factor_notes.get(factor),
        ))

    return PickScoreResponse(
        product_id=product.product_id,
        score=int(max(0, min(100, round(weighted_sum)))),
        mode=mode,
        breakdown=breakdown,
        flags={
            "gpu_score_is_proxy": gpu_is_proxy,
            "price_unavailable": product.price == 0.0,
        },
    )
