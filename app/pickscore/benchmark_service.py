import time
from typing import Optional
from rapidfuzz import process, fuzz
import re, unicodedata

_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300
CONFIDENCE_THRESHOLD = 0.85

# Strip these as characters BEFORE NFKD: U+2122 (TM) decomposes to the letters
# "TM" and superscript digits decompose to real digits, so folding first glues
# the junk into the model name instead of removing it ("RTX™" -> "RTXTM",
# "RTX⁴" -> "RTX4"). Superscripts here are mojibake for a trademark sign the
# scraper mangled, not part of the name.
_JUNK = dict.fromkeys(map(ord, "®™©℠⁰¹²³⁴⁵⁶⁷⁸⁹\u2018\u2019\u201c\u201d"), None)

def _normalize(s: str) -> str:
    s = s.translate(_JUNK)
    s = unicodedata.normalize("NFKD", s)      # Xᵉ -> Xe, and other modifiers
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bprocessor\b", " ", s)
    return s.strip().lower()


def resolve_benchmark(
    model_string: str,
    benchmarks: list[tuple[str, int]],
) -> dict:
    """
    Fuzzy-matches model_string against the benchmarks list.
    Returns: {score: int|None, match_confidence: float, is_proxy: bool}
    """
    # Placeholder from the scraper, not a part name. Without this it fuzzy-matches
    # to whatever is nearest and returns a real-looking score.
    if not model_string or model_string.strip().lower() in {"unknown", "n/a", "none", "-"}:
        return {"score": None, "match_confidence": 0.0, "is_proxy": False}
    
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

    # WRatio does no preprocessing, so a lowercased key was being matched
    # against mixed-case table names -- the same pair scores 95-100 with
    # consistent casing and 0.64 without. Normalize both sides and map back.
    by_norm = {_normalize(name): name for name, _ in benchmarks}
    score_map = {name: score for name, score in benchmarks}

    match = process.extractOne(key, list(by_norm), scorer=fuzz.WRatio)

    if match:
        matched_key, raw_confidence, _ = match
        matched_name = by_norm[matched_key]
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
