import time
from typing import Optional
from rapidfuzz import process, fuzz
import re, unicodedata

# Keyed on (normalized model string, table fingerprint) -- NOT the string
# alone. resolve_benchmark serves both the CPU and the GPU table, so a
# string-only key returned the first caller's mark to the second for the whole
# TTL. "AMD Ryzen Z1 Extreme" is in both real tables (cpu_mark 24613, gpu_mark
# 6428) and _score_cpu runs before _score_gpu, so that GPU scored 24613 -- 3.8x
# its real mark. It also made unrelated tests pass alone and fail together,
# three times, because each test's small table poisoned the next one's lookups.
_cache: dict[tuple[str, int], tuple[dict, float]] = {}
# The key already holds the whole table, so an entry can never be wrong - the
# TTL only bounds memory. It was 5 minutes, which re-ran every fuzzy match on
# the next request after each expiry.
CACHE_TTL = 6 * 3600
# Per table (same fingerprint as the key): normalized name -> real name, and
# name -> mark. Built once per table version instead of once per cache miss,
# which was ~6,000 _normalize calls for every CPU string not seen yet.
_indexes: dict[int, tuple[dict[str, str], dict[str, int]]] = {}
# One threshold per table. The single shared constant was raised 0.6 -> 0.85 in
# August on GPU evidence (four measured mismatches, ADR-0010). The CPU evidence
# points elsewhere and was never gathered until 2026-09-15: across 98 distinct
# active processor_model strings, every match at 0.88 or above was correct with
# cosmetic differences ("Apple M5 (10-core)" -> "Apple M5 10 Core"), and every
# match at EXACTLY 0.85 was a wrong part -- 12 laptops, all Qualcomm, worst
# case "Snapdragon X2 Elite (18-core) X2E88100" -> "AMD Athlon 64 X2 4200+",
# mark 767, the catalog floor, on a 2025 flagship. "X2" matched "X2".
#
# The GPU value is deliberately NOT moved here: whether 0.90 suits it is
# unexamined, and splitting the constant is what makes that a separate
# question instead of a side effect. See ADR-0016.
CPU_CONFIDENCE_THRESHOLD = 0.90
GPU_CONFIDENCE_THRESHOLD = 0.85

# Back-compat alias. Points at the GPU value, which is what this name has meant
# since August.
CONFIDENCE_THRESHOLD = GPU_CONFIDENCE_THRESHOLD

_JUNK = dict.fromkeys(map(ord, "®™©℠⁰¹²³⁴⁵⁶⁷⁸⁹\u2018\u2019\u201c\u201d"), None)

# Apple writes "M5 (10-core)"; PassMark writes "Apple M5 10 Core". Same part,
# 0.882 apart -- correct, and gated once CPU confidence went to 0.90
# (ADR-0016), which cost 9 laptops a right answer.
#
# This is a FORM normalization, not a noise strip like `Processor`. The core
# count is DISCRIMINATING: M5 10-core and M5 Pro 14-core are different parts
# with different marks, so the three spellings must converge without 10 and 14
# converging. Same lesson as ` GPU` and `Laptop` in ADR-0010, applied to a
# spelling rather than to a word.
#
# Anchored on the word `core`, never on the hyphen: a blanket hyphen-to-space
# would split i7-14650hx and re-match half the catalog. The hyphen class is
# ASCII, non-breaking and en dash -- the same three _APPLE_KEY translates for
# the GPU map.
#
# A second pass must be a no-op (found by Hypothesis, test_properties.py). The
# rewrite inserts spaces and removes parens, so anything a later pass would
# match differently has to be matched the same way the first time:
#   - A paren must TOUCH the count or `core`. `\(?\s*` / `\s*\)?` let the spaces
#     this rewrite inserts bring a leftover paren within reach of the next
#     pass: '(10-core))' -> '10 core )' -> '10 core'.
#   - The separator is a hyphen (optionally spaced) OR any whitespace run, not
#     only a literal space. The collapse below turns every whitespace into a
#     space anyway, so '10<tab>core' was matched on the second pass and not the
#     first. Hyphen handling is unchanged: still anchored on `core`.
_CORE_COUNT = re.compile(r"\(?(\d+)(?:\s*[-\u2011\u2013]\s*|\s+)core\)?")

# Qualcomm writes one part number four ways: X1E-78-100 and X1E78100 (both in
# PassMark), X1E 78 100 (ASUS), X1-26-100 (HP). Joined to one token, on both
# sides, so the part number can be matched exactly (see resolve_benchmark).
# x1/x2 + optional E/P + exactly 2+3 digits: "Athlon 64 X2 4200+" and
# "Adreno X1-45" do not match. Measured 2026-10-25: changes 4 CPU-table rows,
# 0 GPU-table rows, 5 catalog strings, 3 _INTEGRATED_GPU_BY_CPU keys.
_QUALCOMM_PART = re.compile(r"\b(x[12][ep]?)[ \-\u2011\u2013]*(\d{2})[ \-\u2011\u2013]*(\d{3})\b")


def _normalize(s: str) -> str:
    s = s.translate(_JUNK)
    s = unicodedata.normalize("NFKD", s)    
    s = s.lower()
    s = re.sub(r"\bprocessor\b", " ", s)
    # AFTER lower(), so "Core"/"CORE" are already folded and one pattern
    # covers every casing. BEFORE the whitespace collapse below, because this
    # injects spaces around the count and depends on that collapse to tidy
    # them -- run after, it would leave "apple m5  10 core " and break the
    # idempotence test.
    s = _CORE_COUNT.sub(r" \1 core ", s)
    # Again, after the rewrite: its inserted spaces can hand the strip above a
    # word boundary it did not have -- 'coreprocessor' becomes 'core processor'
    # -- which a second pass would then act on. A word strip is idempotent, so
    # repeating it is safe; moving it instead would re-open the mirror case
    # '(10 processor core)', which needs the strip BEFORE the rewrite.
    s = re.sub(r"\bprocessor\b", " ", s)
    s = re.sub(r"\s+", " ", s)
    # After the collapse: it only removes separators, so it adds no whitespace
    # for a second pass to act on.
    s = _QUALCOMM_PART.sub(r"\1\2\3", s)
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
    # PassMark has only one generic "Intel Graphics" row (3253) for every
    # Panther Lake iGPU Intel names "Intel® Graphics", whatever its Xe-core
    # count, so these are likely under-scored -- the 4-Xe 356H most of all.
    "core ultra 9 386h": "Intel Graphics",
    "core ultra 7 355": "Intel Graphics",
    "core ultra 5 325": "Intel Graphics",
    "core ultra 5 322": "Intel Graphics",    # ARK 245716: GPU Name "Intel® Graphics", 2 Xe-cores
    "core ultra 7 356h": "Intel Graphics",   # ARK 245532: GPU Name "Intel® Graphics", 4 Xe-cores; likely under-scored, see above

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
    "core 3 100u": "Intel Graphics",         # ARK 236776: GPU Name "Intel® Graphics", 64 EU

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
    "ryzen ai 5 435": "Radeon 840M",       # AMD product page: Graphics Model "AMD Radeon™ 840M"
    "ryzen ai 7 pro 450": "Radeon 860M",   # AMD product page: Graphics Model "AMD Radeon™ 860M"
    "ryzen ai max pro 385": "Radeon 8050S", # AMD product page: Graphics Model "Radeon 8050S Graphics"
    "ryzen 5 pro 230": "Radeon 760M",      # AMD product page: Graphics Model "AMD Radeon™ 760M"

    # --- AMD, Ryzen 7000-series mobile and rebrands ----------------------
    "ryzen 7 7730u": "Ryzen 7 7730U with Radeon Graphics",
    "ryzen 5 7530u": "Ryzen 5 7530U with Radeon Graphics",
    "ryzen 5 7430u": "Ryzen 5 7430U with Radeon Graphics",
    "ryzen 5 7520u": "Radeon 610M",
    "ryzen 3 7320u": "Radeon 610M",
    "ryzen 5 220": "Radeon 740M",
    # Generic row (3142), like ryzen 5 150 above. PassMark also has a
    # CPU-specific "Radeon 660M Ryzen 5 7535HS" row (3087, 1.7% lower).
    "ryzen 5 7535hs": "Radeon 660M",       # AMD product page: Graphics Model "AMD Radeon™ 660M"
    # No AMD product page exists (404); the spec block on AMD's support page.
    "athlon silver 7120u": "Radeon 610M",  # amd.com support page: Graphics Model "AMD Radeon™ 610M"

    # --- Qualcomm Snapdragon X / X2 --------------------------------------
    "snapdragon x x1 26 100": "Qualcomm Adreno X1-45 GPU",
    "snapdragon x plus x1p 42 100": "Qualcomm Adreno X1-45 GPU",
    "snapdragon x elite x1e 78 100": "Qualcomm Adreno X1-85 GPU",
    "snapdragon x2 elite": "Qualcomm Adreno X2-90 GPU",
}

_LAPTOP_SUFFIX = " laptop gpu"

# Words naming only who made the part. ADR-0010's rule is to drop words that
# carry no discriminating information and keep the ones that do: these three
# say nothing a model number does not already say, while GeForce, Radeon and
# Arc name product families and "Laptop" / the trailing " GPU" name different
# parts (Intel Arc 140T and Intel Arc 140T GPU are 17% apart), so those stay.
_VENDOR_WORDS = frozenset({"nvidia", "amd", "intel"})


def _variant_key(s: str) -> str:
    """The canonical form both sides of the rewrite are compared on.

    Without this the comparison was against the raw normalized string, so
    "nvidia geforce rtx 4050" never equalled "geforce rtx 4050" and the rewrite
    silently did not fire for any vendor-prefixed name. The string then reached
    the fuzzy matcher and landed on "RTX PRO 2000 Blackwell Embedded GPU" --
    not the desktop variant of the right part, an unrelated one, which is worse
    than the collisions the rewrite exists to fix. Today's catalog is safe only
    by coincidence: Acer writes bare names and ASUS writes suffixed ones, so no
    row is currently both prefixed and bare.
    """
    return " ".join(w for w in _normalize(s).split() if w not in _VENDOR_WORDS)


_GPU_VARIANT_OVERRIDES: dict[str, str] = {
    "geforce rtx 3050": "GeForce RTX 3050 4GB Laptop GPU",
}

def _laptop_variant(key: str, benchmarks: list[tuple[str, int]]) -> Optional[str]:
    """
    The laptop row that means the same part as `key`, or None.

    Match is exact on the canonical form, not fuzzy: "geforce rtx 5070" must
    equal "geforce rtx 5070 ti laptop gpu" minus the suffix to win, and it
    doesn't -- the Ti is a different part. Dropping the vendor word is not a
    loosening: it is applied to BOTH sides and removes a word that identifies
    nothing. Relaxing to a prefix or substring match instead is how "rtx 5070"
    would start winning "rtx 5070 ti laptop gpu".
    """
    canonical = _variant_key(key)
    if canonical in _GPU_VARIANT_OVERRIDES:
        return _GPU_VARIANT_OVERRIDES[canonical]
    for name, _ in benchmarks:
        norm = _normalize(name)
        if norm.endswith(_LAPTOP_SUFFIX) and _variant_key(norm[: -len(_LAPTOP_SUFFIX)]) == canonical:
            return name
    return None


# Keys normalized the same way as the CPU string they are looked up in, so
# HP's "Snapdragon X X1-26-100" finds "snapdragon x x1 26 100". Longest first.
_INTEGRATED_KEYS = sorted(
    ((_normalize(k), v) for k, v in _INTEGRATED_GPU_BY_CPU.items()),
    key=lambda kv: len(kv[0]), reverse=True,
)


def _integrated_gpu_for(cpu_model: str) -> Optional[str]:
    key = _normalize(cpu_model or "")
    for cpu_key, gpu_name in _INTEGRATED_KEYS:
        if cpu_key in key:
            return gpu_name
    return None


_PLACEHOLDERS = {"unknown", "n/a", "none", "-"}


def _is_placeholder(model_string: str) -> bool:
    return not model_string or model_string.strip().lower() in _PLACEHOLDERS


def _index(table_key: int, benchmarks: list[tuple[str, int]]) -> tuple[dict[str, str], dict[str, int]]:
    if table_key not in _indexes:
        _indexes[table_key] = ({_normalize(name): name for name, _ in benchmarks},
                               {name: score for name, score in benchmarks})
    return _indexes[table_key]


def resolve_benchmark(
    model_string: str,
    benchmarks: list[tuple[str, int]],
    threshold: float = CPU_CONFIDENCE_THRESHOLD,
) -> dict:
    """
    Fuzzy-matches model_string against the benchmarks list.
    Returns: {score: int|None, match_confidence: float, is_proxy: bool}

    `threshold` defaults to the CPU value because every DIRECT caller of this
    function is resolving a CPU (_score_cpu, and get_laptop_ranges' cpu_mark).
    GPU resolution goes through resolve_gpu_benchmark, which passes
    GPU_CONFIDENCE_THRESHOLD explicitly on each of its calls. A new direct
    caller resolving a GPU must pass it too -- the default is a convenience for
    the common case, not a statement that 0.90 is right for both.
    """
    # Placeholder from the scraper, not a part name. Without this it fuzzy-matches
    # to whatever is nearest and returns a real-looking score.
    if _is_placeholder(model_string):
        return {"score": None, "match_confidence": 0.0, "is_proxy": False}
    
    key = _normalize(model_string)
    # The whole table, not its length or its first row: the CPU and GPU tables
    # can hold the same name with different marks, which is the entire bug, and
    # a cheaper fingerprint collides on exactly the small tables tests use.
    # The threshold belongs in the key for the same reason the table does: the
    # same string against the same table resolves differently under 0.85 and
    # 0.90, so leaving it out would recreate the collision fixed one commit ago
    # in a new dimension.
    table_key = hash(tuple(benchmarks))
    cache_key = (key, table_key, threshold)
    now = time.time()

    if cache_key in _cache:
        cached_result, cached_at = _cache[cache_key]
        if (now - cached_at) < CACHE_TTL:
            return cached_result

    if not benchmarks:
        result: dict = {"score": None, "match_confidence": 0.0, "is_proxy": False}
        _cache[cache_key] = (result, now)
        return result

    # WRatio does no preprocessing, so a lowercased key was being matched
    # against mixed-case table names -- the same pair scores 95-100 with
    # consistent casing and 0.64 without. Normalize both sides and map back.
    by_norm, score_map = _index(table_key, benchmarks)

    # A Qualcomm part number is an identity, so it is matched exactly, before
    # and instead of the fuzzy match. Fuzzy cannot reach the threshold here:
    # PassMark's rows carry a " - Qualcomm Oryon" suffix, and WRatio scales any
    # candidate 1.5x longer than the query by 0.9, capping X126100 / X1P42100 /
    # X2E88100 at 0.855 -- tied with "Snapdragon 435" and "Athlon 64 X2". Same
    # rule as _laptop_variant: exact, never fuzzy. When several rows carry the
    # number, the scorer picks among those rows only, in sorted order so a tie
    # is deterministic: X1E78100 has the curated PassMark entry (id 6095,
    # "Qualcomm Snapdragon X Elite - X1E-78-100") and an uncurated row from a
    # truncated name string (id 7089, "... - Qualcomm Or"); 6095 wins.
    part = _QUALCOMM_PART.search(key)
    if part:
        token = re.compile(rf"\b{part.group(0)}\b")
        same = sorted(n for n in by_norm if token.search(n))
        if same:
            matched_name = by_norm[process.extractOne(key, same, scorer=fuzz.WRatio)[0]]
            result = {
                "score": score_map[matched_name],
                "matched_name": matched_name,
                "match_confidence": 1.0,
                "is_proxy": False,
            }
            _cache[cache_key] = (result, now)
            return result

    match = process.extractOne(key, list(by_norm), scorer=fuzz.WRatio)

    if match:
        matched_key, raw_confidence, _ = match
        matched_name = by_norm[matched_key]
        confidence = raw_confidence / 100.0
        if confidence >= threshold:
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

    _cache[cache_key] = (result, now)
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

    `resolution` says which path produced the mark (direct / laptop_variant /
    apple_equivalent / integrated / unresolved); the engine's _GPU_NOTES is
    keyed on it. Copies, never mutations: resolve_benchmark's dicts are cached.
    """
    if has_anchor_token(gpu_model):
        key = _normalize(gpu_model)
        if _LAPTOP_SUFFIX.strip() not in key:
            variant = _laptop_variant(key, benchmarks)
            if variant:
                return _resolved(resolve_benchmark(variant, benchmarks, GPU_CONFIDENCE_THRESHOLD), "laptop_variant")
        return _resolved(resolve_benchmark(gpu_model, benchmarks, GPU_CONFIDENCE_THRESHOLD), "direct")

    apple_name = _APPLE_GPU_EQUIVALENT.get(
        _normalize(gpu_model).translate(_APPLE_KEY)
    )
    if apple_name:
        result = resolve_benchmark(apple_name, benchmarks, GPU_CONFIDENCE_THRESHOLD)
        if result["score"] is not None:
            return {**result, "is_proxy": True, "resolution": "apple_equivalent"}

    igpu_name = _integrated_gpu_for(cpu_model)
    if igpu_name:
        result = resolve_benchmark(igpu_name, benchmarks, GPU_CONFIDENCE_THRESHOLD)
        if result["score"] is not None:
            return {**result, "is_proxy": True, "resolution": "integrated"}

    return {"score": None, "match_confidence": 0.0, "is_proxy": True, "resolution": "unresolved"}


def _resolved(result: dict, path: str) -> dict:
    """An anchored lookup that found no mark is unresolved, whatever path it took."""
    return {**result, "resolution": path if result["score"] is not None else "unresolved"}


# --- Diagnosis (admin Pick Score page) ---------------------------------------
# Read-only: nothing here feeds scoring. Same key, same index, same scorer as
# resolve_benchmark, so the rows shown are the rows the matcher weighed; a unit
# test holds closest[0] to the matcher's choice whenever a string resolves.

def closest_benchmarks(
    model_string: str,
    benchmarks: list[tuple[str, int]],
    threshold: float = CPU_CONFIDENCE_THRESHOLD,
    limit: int = 3,
) -> dict:
    """The PassMark rows nearest to model_string, as resolve_benchmark sees them.

    kind: "placeholder" (not a part name, no candidates), "qualcomm_part" (exact
    part-number rows, confidence 1.0 like the matcher), "qualcomm_part_missing"
    (no row carries the part number; fuzzy candidates follow) or "fuzzy".
    """
    if _is_placeholder(model_string):
        return {"kind": "placeholder", "message": "Placeholder, not a part name",
                "candidates": [], "threshold": threshold}

    key = _normalize(model_string)
    by_norm, score_map = _index(hash(tuple(benchmarks)), benchmarks)

    def rows(choices, confidence=None):
        return [
            {"name": by_norm[k], "mark": score_map[by_norm[k]],
             "confidence": confidence if confidence is not None else round(s / 100.0, 3)}
            for k, s, _ in process.extract(key, choices, scorer=fuzz.WRatio, limit=limit)
        ]

    kind, message = "fuzzy", None
    part = _QUALCOMM_PART.search(key)
    if part:
        token = re.compile(rf"\b{part.group(0)}\b")
        same = sorted(n for n in by_norm if token.search(n))
        if same:
            return {"kind": "qualcomm_part", "message": None,
                    "candidates": rows(same, 1.0), "threshold": threshold}
        kind = "qualcomm_part_missing"
        message = f"Exact part number {part.group(0).upper()}: no row carries it"

    return {"kind": kind, "message": message, "candidates": rows(list(by_norm)), "threshold": threshold}


def gpu_diagnosis(gpu_model: str, cpu_model: str, benchmarks: list[tuple[str, int]]) -> dict:
    """closest_benchmarks along resolve_gpu_benchmark's own path: the laptop-
    variant rewrite for named parts, then the Apple map, then the CPU's iGPU.
    A GPU name with no model number has no meaningful closest rows; what is
    missing there is a map entry."""
    if has_anchor_token(gpu_model):
        key = _normalize(gpu_model)
        variant = _laptop_variant(key, benchmarks) if _LAPTOP_SUFFIX.strip() not in key else None
        return closest_benchmarks(variant or gpu_model, benchmarks, GPU_CONFIDENCE_THRESHOLD)

    apple_name = _APPLE_GPU_EQUIVALENT.get(_normalize(gpu_model).translate(_APPLE_KEY))
    if apple_name:
        return closest_benchmarks(apple_name, benchmarks, GPU_CONFIDENCE_THRESHOLD)
    igpu_name = _integrated_gpu_for(cpu_model)
    if igpu_name:
        return closest_benchmarks(igpu_name, benchmarks, GPU_CONFIDENCE_THRESHOLD)
    return {"kind": "no_igpu_entry", "message": f"No integrated-GPU entry for CPU {cpu_model or 'unknown'}",
            "candidates": [], "threshold": GPU_CONFIDENCE_THRESHOLD}
