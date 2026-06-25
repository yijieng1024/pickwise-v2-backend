import time
from typing import Optional
from rapidfuzz import process, fuzz

_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 minutes
CONFIDENCE_THRESHOLD = 0.6


def _normalize(model_string: str) -> str:
    return model_string.lower().strip()


def resolve_benchmark(
    model_string: str,
    benchmarks: list[tuple[str, int]],
) -> dict:
    """
    Fuzzy-matches model_string against the benchmarks list.
    Returns: {score: int|None, match_confidence: float, is_proxy: bool}
    """
    key = _normalize(model_string)
    now = time.time()

    if key in _cache:
        cached_result, cached_at = _cache[key]
        if (now - cached_at) < CACHE_TTL:
            return cached_result

    if not benchmarks:
        result: dict = {"score": None, "match_confidence": 0.0, "is_proxy": False}
        _cache[key] = (result, now)
        return result

    names = [name for name, _ in benchmarks]
    score_map = {name: score for name, score in benchmarks}

    match = process.extractOne(key, names, scorer=fuzz.WRatio)

    if match:
        matched_name, raw_confidence, _ = match
        confidence = raw_confidence / 100.0
        if confidence >= CONFIDENCE_THRESHOLD:
            result = {
                "score": score_map[matched_name],
                "match_confidence": confidence,
                "is_proxy": False,
            }
        else:
            result = {"score": None, "match_confidence": confidence, "is_proxy": False}
    else:
        result = {"score": None, "match_confidence": 0.0, "is_proxy": False}

    _cache[key] = (result, now)
    return result
