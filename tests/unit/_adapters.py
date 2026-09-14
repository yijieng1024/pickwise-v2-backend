"""
THE ONLY FILE THAT TOUCHES PRODUCTION IMPORTS.

Every test in tests/unit/ calls the wrappers below instead of importing from
`app.*` directly. So when a real signature turns out to be different from what
was assumed here, exactly one file changes and none of the actual assertions
are touched.

Reconciled against the real source 2026-09-14. Every place the original guess
was wrong is marked `REAL:` with what the production symbol actually is.
"""

import sys
import uuid
from pathlib import Path

# run_eval.py is a script under eval/, not a package.
_EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

# --------------------------------------------------------------------------
# Benchmark resolution - app/pickscore/benchmark_service.py
# --------------------------------------------------------------------------
from app.pickscore.benchmark_service import (  # noqa: E402
    _APPLE_GPU_EQUIVALENT,
    _INTEGRATED_GPU_BY_CPU,
    CONFIDENCE_THRESHOLD,
)
from app.pickscore import benchmark_service as _bench  # noqa: E402


def normalize(raw: str) -> str:
    """The string pipeline: strip trademark chars -> NFKD -> lower -> word strip
    -> collapse whitespace. Order is load-bearing; that is what we test."""
    return _bench._normalize(raw)


def has_anchor_token(raw: str) -> bool:
    """True if the string contains at least one token that could identify a
    specific part (a token with a digit, >=3 chars). Anchorless strings must
    never reach the fuzzy matcher.

    REAL: the function is public - `has_anchor_token`, not `_has_anchor_token`.
    """
    return _bench.has_anchor_token(raw)


def strip_laptop_suffix(raw: str) -> str:
    """'rtx 5070 laptop gpu' -> 'rtx 5070'.

    REAL: there is no `_strip_laptop_suffix`. Production does the strip inline
    inside `_laptop_variant`, against the `_LAPTOP_SUFFIX` constant, and only
    accepts an EXACT match on the stripped name. This mirrors that one line so
    the suffix rule stays unit-testable without a benchmark table.
    """
    suffix = _bench._LAPTOP_SUFFIX
    return raw[: -len(suffix)] if raw.endswith(suffix) else raw


def apple_gpu_map() -> dict:
    return _APPLE_GPU_EQUIVALENT


def integrated_gpu_map() -> dict:
    return _INTEGRATED_GPU_BY_CPU


def integrated_lookup(cpu_model: str):
    """Longest-key-first lookup into _INTEGRATED_GPU_BY_CPU.

    REAL: named `_integrated_gpu_for`. Returns the GPU NAME, not a mark -
    resolution to a number happens later, against the benchmark table.
    """
    return _bench._integrated_gpu_for(cpu_model)


def confidence_threshold() -> float:
    return CONFIDENCE_THRESHOLD


# --------------------------------------------------------------------------
# PickScore factors - app/pickscore/engine.py
# --------------------------------------------------------------------------
from app.pickscore import engine as _engine  # noqa: E402
from app.pickscore.schemas import ScorableProduct  # noqa: E402
from app.pickscore.engine import (  # noqa: E402
    DEFAULT_PRIORITY,
    PORTABILITY_MULTIPLIERS,
    PURPOSE_MODIFIERS,
)

# REAL: _KNOWN_PURPOSES does not live in the engine. It is the agent search
# tool's whitelist - which is part of why the label mismatch is invisible:
# the two sets sit in different packages and nothing imports across.
from app.agent.tools.search_laptops import _KNOWN_PURPOSES  # noqa: E402


# REAL: the engine takes a range DICT per factor ({"min","max","values"}),
# keyed "price"/"cpu_mark"/"gpu_mark"/"ram_gb"/..., not the (min, max) tuples
# the `ranges` fixture supplies. Converted here so the fixture stays readable.
# With no "values" list the engine falls back to min-max, which is a real
# documented branch (the benchmark-table fallback in get_laptop_ranges).
_RANGE_KEYS = {"price_rm": "price"}


def _ranges_to_engine(ranges: dict) -> dict:
    out = {}
    for key, val in ranges.items():
        name = _RANGE_KEYS.get(key, key)
        if isinstance(val, dict):
            out[name] = val
        else:
            lo, hi = val
            out[name] = {"min": float(lo), "max": float(hi)}
    return out


def _product(**overrides) -> ScorableProduct:
    base = dict(
        product_id=uuid.uuid4(),
        brand_name="ASUS",
        price=5899.0,
        cpu_model="Intel Core i7-14650HX",
        gpu_model="NVIDIA GeForce RTX 5060 Laptop GPU",
        ram_gb=16,
        storage_gb=1024,
        storage_type="SSD",
        weight_kg=2.2,
        battery_wh=90.0,
        display_size_inch=16.0,
    )
    base.update(overrides)
    return ScorableProduct(**base)


class _Pref:
    """Stand-in for LaptopUserPreference. _score_price reads only .budget."""

    def __init__(self, budget_max):
        self.budget = {"min": None, "max": budget_max}
        self.screen_size = None
        self.brand_preferences = None


def _price_call(price_rm, ranges, budget_max, personalized):
    """REAL: _score_price(product, user_pref, ranges, mode) -> (score, reason).
    Mode is the string "personalized"/"general", not a bool."""
    return _engine._score_price(
        _product(price=float(price_rm)),
        _Pref(budget_max) if personalized else None,
        _ranges_to_engine(ranges),
        "personalized" if personalized else "general",
    )


def score_price(price_rm, ranges, budget_max=None, personalized=False) -> float:
    return float(_price_call(price_rm, ranges, budget_max, personalized)[0])


def score_price_reason(price_rm, ranges, budget_max=None, personalized=False):
    """The reason string, for the 'unknown must not look like average' test."""
    return _price_call(price_rm, ranges, budget_max, personalized)[1]


def score_ram_storage(ram_gb, storage_gb, storage_type, ranges) -> float:
    return float(
        _engine._score_ram_storage(
            _product(ram_gb=ram_gb, storage_gb=storage_gb, storage_type=storage_type),
            _ranges_to_engine(ranges),
        )
    )


def score_cpu(cpu_model, ranges, benchmarks=None) -> float:
    """REAL: _score_cpu(product, ranges, cpu_benchmarks) -> (score, result).
    The benchmark table is a caller-supplied list of (name, mark) tuples, so
    the unit tier passes [] and exercises the unresolved path with no DB."""
    score, _result = _engine._score_cpu(
        _product(cpu_model=cpu_model), _ranges_to_engine(ranges), benchmarks or []
    )
    return float(score)


def score_gpu(gpu_model, ranges, cpu_model="Unknown", benchmarks=None) -> float:
    """REAL: _score_gpu(product, ranges, gpu_benchmarks) -> (score, is_proxy, note)."""
    score, _is_proxy, _note = _engine._score_gpu(
        _product(gpu_model=gpu_model, cpu_model=cpu_model),
        _ranges_to_engine(ranges),
        benchmarks or [],
    )
    return float(score)


def percentile_normalize(value, population) -> float:
    """The shipped normalization curve. Every _score_* funnels through this.

    REAL: _normalize(value, factor_range, inverse=False), where factor_range is
    the {"min","max","values"} dict - the distribution is passed inside it, not
    as a second positional argument."""
    pop = sorted(float(v) for v in population)
    return _engine._normalize(value, {"min": pop[0], "max": pop[-1], "values": pop})


def default_priority() -> dict:
    return DEFAULT_PRIORITY


def purpose_modifiers() -> dict:
    return PURPOSE_MODIFIERS


def portability_multipliers() -> dict:
    return PORTABILITY_MULTIPLIERS


def known_purposes() -> set:
    return set(_KNOWN_PURPOSES)


# --------------------------------------------------------------------------
# Use-case presets - app/laptops/pickscore_general.py
# --------------------------------------------------------------------------
from app.laptops.pickscore_general import USE_CASE_PRIORITIES  # noqa: E402


def use_case_priorities() -> dict:
    return USE_CASE_PRIORITIES


# --------------------------------------------------------------------------
# Price-fabrication detector - lives with the eval harness
# --------------------------------------------------------------------------
# REAL: not in app/rag/evaluation.py. It is `_ungrounded_rm` in eval/run_eval.py
# and takes tool texts and user texts as two separate lists.
import run_eval as _run_eval  # noqa: E402


def ungrounded_prices(response_text: str, tool_outputs) -> list:
    """Returns the RM figures in `response_text` that are not grounded in any of
    `tool_outputs`."""
    return list(_run_eval._ungrounded_rm(response_text, list(tool_outputs), []))


# --------------------------------------------------------------------------
# Relevance gating - app/rag/gating.py
# --------------------------------------------------------------------------
# REAL: the constant is RELEVANCE_THRESHOLD, and there is no passes_gate() -
# gating happens inside relevance_gate() over a candidate list.
from app.rag.gating import RELEVANCE_THRESHOLD  # noqa: E402


def gate_threshold() -> float:
    return RELEVANCE_THRESHOLD


def passes_gate(similarity: float) -> bool:
    return float(similarity) >= RELEVANCE_THRESHOLD


# --------------------------------------------------------------------------
# Constraint relaxation - app/rag/relaxation.py
# --------------------------------------------------------------------------
from app.rag.relaxation import _MIN_VIABLE_SCORE  # noqa: E402


def min_viable_score() -> float:
    return _MIN_VIABLE_SCORE


# --------------------------------------------------------------------------
# Family key
# --------------------------------------------------------------------------
# REAL: moved out of app/reviews/service.py into app/laptops/family_key.py and
# renamed `family_key`, so the review pipeline and the laptop_family grouping
# share one implementation.
from app.laptops.family_key import family_key as _family_key  # noqa: E402


def family_key(product_name: str) -> str:
    return _family_key(product_name)


# --------------------------------------------------------------------------
# Tier 5 — pipeline internals (retrieve -> rerank -> relax -> gate)
# --------------------------------------------------------------------------
# These are re-exported rather than wrapped: the tests drive real dataclasses
# through the real functions, and a wrapper around a dataclass constructor
# would just be a second place for the field list to drift.
from app.rag.reranker import (  # noqa: E402
    RankedCandidate,
    UserConstraints,
    _brand_bonus,
    _budget_penalty,
    _purpose_bonus,
    _weight_penalty,
    rerank,
)
from app.rag.retrieval import (  # noqa: E402
    RetrievalCandidate,
    _relational_fallback,
    retrieve_candidates,
)
from app.rag import retrieval as _retrieval  # noqa: E402
from app.rag.reranker import (  # noqa: E402
    _PURPOSE_CPU_SIGNALS,
    _PURPOSE_GPU_SIGNALS,
)


def purpose_gpu_signals() -> dict:
    """reranker.py:20-23 — the purpose -> GPU-keyword map. Matched as a
    substring against laptop.gpu_model, nothing else."""
    return _PURPOSE_GPU_SIGNALS


def purpose_cpu_signals() -> dict:
    return _PURPOSE_CPU_SIGNALS


def normalize_purpose(purpose):
    """The agent tool's purpose whitelist, the thing that decides which
    reranker signal set a questionnaire answer ends up in."""
    return _search_tool._normalize_purpose(purpose)
from app.rag.gating import relevance_gate  # noqa: E402
from app.rag import relaxation as _relaxation  # noqa: E402
from app.rag.relaxation import needs_relaxation, relax_and_retry  # noqa: E402
# REAL: `from app.agent.tools import search_laptops` gives the StructuredTool
# the package re-exports, not the module. Import it by path.
import importlib  # noqa: E402

_search_tool = importlib.import_module("app.agent.tools.search_laptops")
_MAX_RESULTS = _search_tool._MAX_RESULTS
_run_search = _search_tool._run_search


# The real logger, for the fallback-flag tests: _install_stubs replaces the
# name inside the search tool, and those tests need it back.
from app.rag.evaluation import log_pipeline_result as _real_log_pipeline_result  # noqa: E402


def max_results() -> int:
    return _MAX_RESULTS


def relaxation_steps() -> list:
    """The ordered relaxation plan: weight first, then budget. Brand is absent
    on purpose — it is never auto-relaxed."""
    return _relaxation._RELAXATION_STEPS
