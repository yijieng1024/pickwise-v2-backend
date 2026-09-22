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


def variant_key(raw: str) -> str:
    """The canonical form _laptop_variant compares on: normalized, with vendor
    words that carry no discriminating information removed."""
    return _bench._variant_key(raw)


def laptop_variant(key: str, benchmarks):
    """The desktop -> laptop rewrite itself. `key` is already normalized."""
    return _bench._laptop_variant(key, benchmarks)


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
    """Kept for the existing GPU-side test. The constant is now the GPU one --
    see cpu_confidence_threshold/gpu_confidence_threshold."""
    return _bench.GPU_CONFIDENCE_THRESHOLD


def cpu_confidence_threshold() -> float:
    return _bench.CPU_CONFIDENCE_THRESHOLD


def gpu_confidence_threshold() -> float:
    return _bench.GPU_CONFIDENCE_THRESHOLD


def resolve_cpu(model, benchmarks):
    return _bench.resolve_benchmark(model, benchmarks)


def resolve_gpu(gpu_model, cpu_model, benchmarks):
    return _bench.resolve_gpu_benchmark(gpu_model, cpu_model, benchmarks)


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

# The canonical label set. This used to import _KNOWN_PURPOSES from
# app.agent.tools.search_laptops, which reaches a set of strings through the
# entire agent stack: app/agent/tools/__init__.py -> laptop_tools ->
# app.database -> create_engine() at module scope. That is why the unit tier
# could not collect without a DATABASE_URL, masked the whole time by a local
# .env.
#
# Since the label unification, _KNOWN_PURPOSES IS set(PURPOSES), and
# app/purposes.py imports nothing -- it was built that way so every consumer
# could read it without a cycle. So this is not a workaround for the import
# problem; the agent tool was always the wrong module to ask.
from app.purposes import PURPOSES  # noqa: E402

_KNOWN_PURPOSES = set(PURPOSES)


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
    """Stand-in for LaptopUserPreference. _score_price reads only .budget;
    _score_screen_size and _score_brand read the other two."""

    def __init__(self, budget_max=None, screen_size=None, brand_preferences=None):
        self.budget = {"min": None, "max": budget_max}
        self.screen_size = screen_size
        self.brand_preferences = brand_preferences


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


def score_portability(weight_kg, ranges) -> float:
    return float(_engine._score_portability(_product(weight_kg=weight_kg), _ranges_to_engine(ranges)))


def score_battery(battery_wh, ranges) -> float:
    return float(_engine._score_battery(_product(battery_wh=battery_wh), _ranges_to_engine(ranges)))


def score_screen_size(display_size_inch, screen_size_pref) -> float:
    """Personalized mode; screen_size_pref is the questionnaire's answer string."""
    return float(_engine._score_screen_size(
        _product(display_size_inch=display_size_inch),
        _Pref(screen_size=[screen_size_pref]),
        "personalized",
    ))


def score_brand(brand_name, brand_preferences) -> float:
    return float(_engine._score_brand(
        _product(brand_name=brand_name),
        _Pref(brand_preferences=list(brand_preferences)),
        "personalized",
    ))


def score_cpu(cpu_model, ranges, benchmarks=None) -> float:
    """REAL: _score_cpu(product, ranges, cpu_benchmarks) -> (score, flags).
    The benchmark table is a caller-supplied list of (name, mark) tuples, so
    the unit tier passes [] and exercises the unresolved path with no DB."""
    return float(score_cpu_flagged(cpu_model, ranges, benchmarks)[0])


def score_cpu_flagged(cpu_model, ranges, benchmarks=None):
    """(score, flags). flags carries cpu_benchmark_unresolved."""
    score, flags = _engine._score_cpu(
        _product(cpu_model=cpu_model), _ranges_to_engine(ranges), benchmarks or []
    )
    return float(score), flags


def score_gpu(gpu_model, ranges, cpu_model="Unknown", benchmarks=None) -> float:
    """REAL: _score_gpu(product, ranges, gpu_benchmarks) -> (score, flags, note)."""
    return float(score_gpu_flagged(gpu_model, ranges, cpu_model, benchmarks)[0])


def score_gpu_flagged(gpu_model, ranges, cpu_model="Unknown", benchmarks=None):
    """(score, flags). flags carries gpu_score_is_proxy AND
    gpu_benchmark_unresolved -- the two are different questions: an Apple or
    integrated GPU is a proxy that DID resolve, while an unresolved one is a
    fabricated neutral."""
    score, flags, _note = _engine._score_gpu(
        _product(gpu_model=gpu_model, cpu_model=cpu_model),
        _ranges_to_engine(ranges),
        benchmarks or [],
    )
    return float(score), flags


def pick_score(cpu_model, gpu_model, ranges, cpu_benchmarks=None, gpu_benchmarks=None,
               price=5899.0):
    """The whole engine, general mode. Returns the PickScoreResponse."""
    return _engine.calculate_pick_score(
        _product(cpu_model=cpu_model, gpu_model=gpu_model, price=price),
        None,
        _ranges_to_engine(ranges),
        cpu_benchmarks or [],
        gpu_benchmarks or [],
    )


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
from app.rag.reranker import _PURPOSE_CPU_SIGNALS  # noqa: E402


def purpose_cpu_signals() -> dict:
    """The purpose -> CPU-keyword map. The GPU half was removed; see the
    reranker module docstring."""
    return _PURPOSE_CPU_SIGNALS


def normalize_purpose(purpose):
    """The agent tool's purpose handling: returns a one-element list of the
    canonical label, [] for no purpose, and raises ValueError on anything it
    does not recognise."""
    return _search_tool._normalize_purpose(purpose)


def use_case_slugs() -> dict:
    """The canonical purpose -> DB/URL slug map (app/purposes.py)."""
    from app.purposes import USE_CASE_SLUGS

    return USE_CASE_SLUGS
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


def fallback_similarity() -> float:
    """The placeholder similarity _relational_fallback stamps on every row."""
    return _retrieval._FALLBACK_SIMILARITY


def max_results() -> int:
    return _MAX_RESULTS


def relaxation_steps() -> list:
    """The ordered relaxation plan: weight first, then budget. Brand is absent
    on purpose — it is never auto-relaxed."""
    return _relaxation._RELAXATION_STEPS


# --------------------------------------------------------------------------
# Background job cancellation - app/common/job_model.py, app/common/job_service.py
# --------------------------------------------------------------------------
from app.common.job_model import BackgroundJob, JobRead, JobStatus  # noqa: E402
from app.common.job_service import JobProgress  # noqa: E402


def job_status():
    """The status vocabulary. CANCELLING is a request; CANCELLED is an outcome."""
    return JobStatus


def make_job(**kwargs) -> BackgroundJob:
    """A BackgroundJob row, unsaved — nothing here touches a database."""
    kwargs.setdefault("job_type", "processor.process_pending")
    return BackgroundJob(**kwargs)


def read_job(job: BackgroundJob) -> JobRead:
    """The polling payload, including the derived progress_percentage."""
    return JobRead.from_job(job)


def progress_seeing_status(status: str) -> JobProgress:
    """
    A JobProgress whose last bookkeeping write saw *status* on the row.

    `_status` is set inside `_update`, which needs a database. Setting it
    directly is what lets the cancel flag be tested without one — the thing
    under test is how a worker reads it, not how it got there.
    """
    progress = JobProgress(uuid.uuid4())
    progress._status = status
    return progress


# --------------------------------------------------------------------------
# Auth email + reset tokens - app/users/email.py, app/users/auth.py
# --------------------------------------------------------------------------
# These two are exposed as MODULES rather than wrapped call-by-call, and the
# reason is specific: both read `settings` at call time (a secret the unit tier
# does not have) and `email` calls `httpx.post`. The tests have to replace
# those names inside the module, which needs the module object. Everything
# else they touch is wrapped below, so a signature change still lands here.
from app.users import auth as _users_auth  # noqa: E402
from app.users import email as _users_email  # noqa: E402


def email_module():
    """app/users/email.py, for monkeypatching `settings` and `httpx`."""
    return _users_email


def auth_module():
    """app/users/auth.py, for monkeypatching `settings`."""
    return _users_auth


def password_reset_fingerprint(password_hash):
    """The HMAC of the account's current password hash that makes a reset link
    single-use. Accepts None (Google-only accounts store no hash)."""
    return _users_auth.password_reset_fingerprint(password_hash)


def create_password_reset_token(email: str, password_hash) -> str:
    return _users_auth.create_password_reset_token(email, password_hash)


def verify_password_reset_token(token: str):
    """REAL: returns `(email, fingerprint)` or None -- not the bare email it
    returned before the single-use change."""
    return _users_auth.verify_password_reset_token(token)


def create_email_verification_token(email: str) -> str:
    return _users_auth.create_email_verification_token(email)


# --------------------------------------------------------------------------
# Auth-endpoint throttling - app/common/http_rate_limit.py
# --------------------------------------------------------------------------
# NOT app/common/rate_limit.py, which paces outbound Gemini calls. This one
# throttles inbound HTTP. Exposed as a module too: the window tests replace
# `time.monotonic` inside it.
from app.common import http_rate_limit as _http_rate_limit  # noqa: E402


def rate_limit_module():
    return _http_rate_limit


# --------------------------------------------------------------------------
# Variant image filtering - app/processor/engine.py
# --------------------------------------------------------------------------
# Safe to import with no configuration: engine.py's module scope builds only a
# regex, a logger and the Gemma rate limiter (plain arithmetic). `settings` is
# read inside the functions, never at import.
from app.processor.engine import _filter_variant_images as _filter_images  # noqa: E402


def filter_variant_images(urls, display_size_inch):
    """Drops gallery photos belonging to a different size of the same model,
    and social-preview cards. Falls back to the full list when the size filter
    would empty it -- a vendor that ships the 14" photo on the 16" SKU must not
    end up with no images at all."""
    return _filter_images(urls, display_size_inch)
