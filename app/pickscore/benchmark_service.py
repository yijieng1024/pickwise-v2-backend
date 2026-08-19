import time
from typing import Optional
from rapidfuzz import process, fuzz
import re, unicodedata

_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300
CONFIDENCE_THRESHOLD = 0.85

_JUNK = dict.fromkeys(map(ord, "®™©℠⁰¹²³⁴⁵⁶⁷⁸⁹\u2018\u2019\u201c\u201d"), None)

def _normalize(s: str) -> str:
    s = s.translate(_JUNK)
    s = unicodedata.normalize("NFKD", s)    
    s = s.lower()
    s = re.sub(r"\bprocessor\b", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def has_anchor_token(model_string: str) -> bool:
    key = _normalize(model_string or "").replace("-", " ")
    return any(any(c.isdigit() for c in t) and len(t) >= 3 for t in key.split())

# PassMark has no entries for ARM Apple GPUs, so these strings cannot be
# resolved directly. Each value is the NVIDIA/Intel laptop part that scored
# closest to the Apple GPU on a benchmark BOTH have run (3DMark Steel Nomad
# and Steel Nomad Light, via Notebookcheck) -- a cross-architecture estimate,
# not a measurement of the same silicon.
#
# Keyed on the GPU string rather than the CPU, unlike _INTEGRATED_GPU_BY_CPU:
# Apple's marketing name already states the core count, which is what varies.
_APPLE_GPU_EQUIVALENT: dict[str, str] = {
    # Measured on both sides -- Apple score vs anchor score, Steel Nomad Light
    "40 core gpu": "GeForce RTX 5070 Ti Laptop GPU",  # 17022 vs 17324 (-1.7%), Steel Nomad 4158 vs 3865
    "20 core gpu": "GeForce RTX 4060 Laptop GPU",     # 10018 vs 9584 (+4.5%); RTX 3080 Laptop 10323 is a near tie the other way
    "10 core gpu": "Qualcomm Adreno X2-90 GPU",       # Steel Nomad 1125 vs 1180
    "8 core gpu":  "Intel Arc 140T GPU",           # Steel Nomad 1030 vs 1025

    # Derived, not measured -- re-check when the sources publish real data
    "32 core gpu": "GeForce RTX 5070 Laptop GPU",     # nanoreview ratio (-13.7% vs 40-core) on a Notebookcheck scale
    "16 core gpu": "GeForce RTX 5050 Laptop GPU",     # no published benchmark; M4 Pro's 16-of-20 cut ratio 0.857 applied to M5 Pro 20-core
    "5 core gpu":  "Intel UHD Graphics",              # A18 Pro sits below every laptop GPU with an SNL score; anchored on Notebookcheck's class placement
}

# Integrated GPUs ship fused to a CPU
_INTEGRATED_GPU_BY_CPU: dict[str, str] = {
    # --- Intel, Arrow Lake-H (Core Ultra series 2) -----------------------
    "core ultra 7 255h": "Intel Arc 140T GPU",
    "core ultra 9 285h": "Intel Arc 140T GPU",
    "core ultra 5 225h": "Intel Arc 130T GPU",

    # --- Intel, Lunar Lake (Core Ultra series 2, V-series) ---------------
    # notebookcheck.net: 256V/258V -> Arc 140V (8 Xe2), 226V -> Arc 130V (7).
    "core ultra 7 256v": "Intel Arc 140V GPU",
    "core ultra 7 258v": "Intel Arc 140V GPU",
    "core ultra 5 226v": "Intel Arc 130V GPU",
    "core ultra 7 165h": "Intel Arc",        # Meteor Lake, 8 Xe-cores; must sit
    "core ultra 5 125h": "Intel Arc",        # below Arc 130T (6122, Arrow Lake H)

    # --- Intel, Panther Lake (Core Ultra series 3) -----------------------
    "core ultra x9 388h": "Intel Arc B390 GPU",
    "core ultra 7 358h": "Intel Arc B390 GPU",
    "core ultra 9 386h": "Intel Graphics",
    "core ultra 7 355": "Intel Graphics",
    "core ultra 5 325": "Intel Graphics",

    # --- Intel, Meteor Lake-U / Arrow Lake-U -----------------------------
    "core ultra 7 255u": "Intel Graphics",
    "core ultra 5 225u": "Intel Graphics",
    "core ultra 5 115u": "Intel Graphics",

    # --- Intel, Raptor Lake-U (and its Core 5/7 rebrands) ----------------
    "core 7 150u": "Intel Iris Xe",
    "core 5 120u": "Intel Iris Xe",
    "core i7-1355u": "Intel Iris Xe",
    "core i5-1335u": "Intel Iris Xe",
    "core i7-1370p": "Intel Iris Xe",

    # --- Intel, Raptor Lake-H and Alder Lake-N ---------------------------
    "core i7-13620h": "Intel UHD Graphics",
    "core i5-13420h": "Intel UHD Graphics",
    "core 5 210h": "Intel UHD Graphics",
    "core 7 240h": "Intel UHD Graphics",
    "intel n100": "Intel UHD Graphics",
    "intel n150": "Intel UHD Graphics",
    "n355": "Intel UHD Graphics",
    "n4500": "Intel UHD Graphics",

    # --- AMD, Ryzen AI 300/400 (Strix, Krackan, Gorgon, Strix Halo) ------
    "ryzen ai 9 hx 370": "Radeon 890M",
    "ryzen ai max+ 395": "Radeon 8060S",
    "ryzen ai 9 465": "Radeon 880M",
    "ryzen ai 7 350": "Radeon 860M",
    "ryzen ai 7 445": "Radeon 840M",
    "ryzen ai 5 430": "Radeon 840M",
    "ryzen ai 5 330": "Radeon 820M",
    "ryzen 5 150": "Radeon 660M",      # Zen 3+ Rembrandt-R, AMD product page

    # --- AMD, Ryzen 7000-series mobile and rebrands ----------------------
    "ryzen 7 7730u": "Ryzen 7 7730U with Radeon Graphics",
    "ryzen 5 7530u": "Ryzen 5 7530U with Radeon Graphics",
    "ryzen 5 7430u": "Ryzen 5 7430U with Radeon Graphics",
    "ryzen 5 7520u": "Radeon 610M",
    "ryzen 3 7320u": "Radeon 610M",
    "ryzen 5 220": "Radeon 740M",

    # --- Qualcomm Snapdragon X / X2 --------------------------------------
    "snapdragon x x1 26 100": "Qualcomm Adreno X1-45 GPU",
    "snapdragon x plus x1p 42 100": "Qualcomm Adreno X1-45 GPU",
    "snapdragon x elite x1e 78 100": "Qualcomm Adreno X1-85 GPU",
    "snapdragon x2 elite": "Qualcomm Adreno X2-90 GPU",
}

_LAPTOP_SUFFIX = " laptop gpu"

_GPU_VARIANT_OVERRIDES: dict[str, str] = {
    "geforce rtx 3050": "GeForce RTX 3050 4GB Laptop GPU",
}

def _laptop_variant(key: str, benchmarks: list[tuple[str, int]]) -> Optional[str]:
    """
    The laptop row that means the same part as `key`, or None.

    Match is exact after removing the suffix, not fuzzy: "geforce rtx 5070"
    must equal "geforce rtx 5070 ti laptop gpu" minus the suffix to win, and
    it doesn't -- the Ti is a different part. Fuzzy matching here would
    reintroduce exactly the ambiguity this function exists to remove.
    """
    if key in _GPU_VARIANT_OVERRIDES:
        return _GPU_VARIANT_OVERRIDES[key]
    for name, _ in benchmarks:
        norm = _normalize(name)
        if norm.endswith(_LAPTOP_SUFFIX) and norm[: -len(_LAPTOP_SUFFIX)] == key:
            return name
    return None


def _integrated_gpu_for(cpu_model: str) -> Optional[str]:
    key = _normalize(cpu_model or "")
    for cpu_key in sorted(_INTEGRATED_GPU_BY_CPU, key=len, reverse=True):
        if cpu_key in key:
            return _INTEGRATED_GPU_BY_CPU[cpu_key]
    return None


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
                "matched_name": matched_name,
                "match_confidence": confidence,
                "is_proxy": False,
            }
        else:
            result = {"score": None, "match_confidence": confidence, "is_proxy": False}
    else:
        result = {"score": None, "match_confidence": 0.0, "is_proxy": False}

    _cache[key] = (result, now)
    return result


# Apple's marketing names carry a core count but no model number, so they
# fail the anchor test like any other marketing string -- but unlike
# "AMD Radeon Graphics" the core count DOES identify the part, so they key on
# the GPU string rather than the CPU. See _APPLE_GPU_EQUIVALENT.
_APPLE_KEY = str.maketrans("-\u2011\u2013", "   ")

def resolve_gpu_benchmark(
    gpu_model: str,
    cpu_model: str,
    benchmarks: list[tuple[str, int]],
) -> dict:
    """
    GPU resolution with an integrated-graphics path.

    Discrete GPUs carry a model number and resolve normally. Anchorless
    strings are routed to a known part instead of being fuzzy-matched on
    marketing words: Apple's core-count names by the GPU string, everything
    else by the CPU's integrated GPU. Anything still unresolved comes back
    is_proxy=True so the gaming ranking can demote it -- an unknown GPU must
    not be able to outrank a measured one.

    The placeholder strings resolve_benchmark rejects ("Unknown", "N/A") are
    anchorless too, so they take the same path: the CPU's iGPU is what that
    machine is at minimum known to have, and it is flagged as a proxy either
    way.
    """
    if has_anchor_token(gpu_model):
        key = _normalize(gpu_model)
        if _LAPTOP_SUFFIX.strip() not in key:
            variant = _laptop_variant(key, benchmarks)
            if variant:
                return resolve_benchmark(variant, benchmarks)
        return resolve_benchmark(gpu_model, benchmarks)

    apple_name = _APPLE_GPU_EQUIVALENT.get(
        _normalize(gpu_model).translate(_APPLE_KEY)
    )
    if apple_name:
        result = resolve_benchmark(apple_name, benchmarks)
        if result["score"] is not None:
            return {**result, "is_proxy": True}

    igpu_name = _integrated_gpu_for(cpu_model)
    if igpu_name:
        result = resolve_benchmark(igpu_name, benchmarks)
        if result["score"] is not None:
            return {**result, "is_proxy": True}

    return {"score": None, "match_confidence": 0.0, "is_proxy": True}
