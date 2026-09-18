"""
Tier 1 -- properties of the PickScore engine, checked by Hypothesis.

The example-based tests pin the values someone thought to write down. These
pin the rules every value must obey, over inputs nobody thought to write down:
bounds, monotonicity, order preservation, idempotence. A counter-example here
is a finding, never a reason to narrow a strategy without saying why.

Default settings on purpose (max_examples=100). If a property is too slow or
too often filtered, the strategy is what shrinks, not the example count.
"""

import math

from hypothesis import assume, example, given
from hypothesis import strategies as st

from ._adapters import (
    normalize,
    percentile_normalize,
    score_battery,
    score_brand,
    score_cpu_flagged,
    score_gpu_flagged,
    score_portability,
    score_price,
    score_ram_storage,
    score_screen_size,
)

finite = st.floats(allow_nan=False, allow_infinity=False)
population = st.lists(finite, min_size=1, max_size=40)
# Benchmark marks are integers in the database. Bounded to a BIGINT so float()
# of one cannot overflow -- a mark beyond that is not a mark.
mark = st.integers(min_value=-(2**63), max_value=2**63 - 1)


@st.composite
def factor_range(draw):
    """Either shape the engine accepts: a percentile distribution, or the bare
    (min, max) pair that the benchmark-table fallback produces."""
    values = draw(population)
    if draw(st.booleans()):
        values = sorted(values)
        return {"min": values[0], "max": values[-1], "values": values}
    lo, hi = draw(finite), draw(finite)
    return (lo, hi)


def _in_bounds(score):
    return 0.0 <= score <= 100.0


_CPU = "Intel Core i7-14650HX"
_GPU = "NVIDIA GeForce RTX 5060 Laptop GPU"
_GPU_ROW = "GeForce RTX 5060 Laptop GPU"


# --------------------------------------------------------------------------
# 1. Every factor lands in [0, 100]
# --------------------------------------------------------------------------


@given(price=finite, r=factor_range(), budget=st.one_of(st.none(), finite))
def test_price_is_bounded(price, r, budget):
    """Catches a price curve that escapes the scale -- e.g. the budget penalty
    going negative, or an over-budget ratio multiplying the base above 100."""
    assert _in_bounds(score_price(price, {"price": r}))
    assert _in_bounds(score_price(price, {"price": r}, budget_max=budget, personalized=True))


@given(m=mark, r=factor_range())
def test_cpu_is_bounded(m, r):
    """Catches a resolved CPU mark scored outside the scale."""
    score, flags = score_cpu_flagged(_CPU, {"cpu_mark": r}, [(_CPU, m)])
    assert not flags["cpu_benchmark_unresolved"], "the property would be vacuous"
    assert _in_bounds(score)


@given(m=mark, r=factor_range())
def test_gpu_is_bounded(m, r):
    """Catches a resolved GPU mark scored outside the scale."""
    score, flags = score_gpu_flagged(_GPU, {"gpu_mark": r}, benchmarks=[(_GPU_ROW, m)])
    assert not flags["gpu_benchmark_unresolved"], "the property would be vacuous"
    assert _in_bounds(score)


@given(
    ram=st.integers(min_value=-(2**63), max_value=2**63 - 1),
    storage=st.integers(min_value=-(2**63), max_value=2**63 - 1),
    storage_type=st.sampled_from(["SSD", "HDD", "hdd + ssd", None]),
    ram_r=factor_range(),
    storage_r=factor_range(),
)
def test_ram_storage_is_bounded(ram, storage, storage_type, ram_r, storage_r):
    """Catches the HDD penalty pushing storage below zero, or the 0.6/0.4 blend
    leaving the scale."""
    score = score_ram_storage(ram, storage, storage_type, {"ram_gb": ram_r, "storage_gb": storage_r})
    assert _in_bounds(score)


@given(weight=finite, r=factor_range())
def test_portability_is_bounded(weight, r):
    """Catches the inverse curve (100 - score) escaping the scale."""
    assert _in_bounds(score_portability(weight, {"weight_kg": r}))


@given(battery=finite, r=factor_range())
def test_battery_is_bounded(battery, r):
    """Catches a battery capacity scored outside the scale."""
    assert _in_bounds(score_battery(battery, {"battery_wh": r}))


@given(size=finite, pref=st.text())
def test_screen_size_is_bounded(size, pref):
    """Catches a bucket distance large enough to push 100 - 40*d below zero
    without the clamp."""
    assert _in_bounds(score_screen_size(size, pref))


@given(brand=st.text(), prefs=st.lists(st.text()))
def test_brand_is_bounded(brand, prefs):
    """Catches a brand bonus outside the scale."""
    assert _in_bounds(score_brand(brand, prefs))


# --------------------------------------------------------------------------
# 2. A better benchmark never scores lower
# --------------------------------------------------------------------------


@given(a=mark, b=mark, r=factor_range())
def test_cpu_is_monotonic_in_its_mark(a, b, r):
    """Catches a CPU curve that inverts anywhere: a faster part scoring below a
    slower one against the same catalog."""
    lo, hi = sorted((a, b))
    s_lo, _ = score_cpu_flagged(_CPU, {"cpu_mark": r}, [(_CPU, lo)])
    s_hi, _ = score_cpu_flagged(_CPU, {"cpu_mark": r}, [(_CPU, hi)])
    assert s_lo <= s_hi


@given(a=mark, b=mark, r=factor_range())
def test_gpu_is_monotonic_in_its_mark(a, b, r):
    """Same, for the GPU curve."""
    lo, hi = sorted((a, b))
    s_lo, _ = score_gpu_flagged(_GPU, {"gpu_mark": r}, benchmarks=[(_GPU_ROW, lo)])
    s_hi, _ = score_gpu_flagged(_GPU, {"gpu_mark": r}, benchmarks=[(_GPU_ROW, hi)])
    assert s_lo <= s_hi


# --------------------------------------------------------------------------
# 3. A cheaper laptop never scores lower on price (general mode)
# --------------------------------------------------------------------------


# 0.0 is not a price: it is the "unknown" sentinel, scored as a flagged neutral
# 50 with a reason (see test_pickscore_factors). Hypothesis found exactly that
# on the unnarrowed strategy -- shrunk to a=0.0, b=1.0, catalog [2.0]: 50 < 100
# -- which is the sentinel doing its job, not the curve inverting. Only that
# one value is removed. (-0.0 == 0.0, so it goes too, as it would in the engine.)
known_price = finite.filter(lambda p: p != 0.0)


@given(a=known_price, b=known_price, r=factor_range())
def test_general_price_is_monotonically_decreasing(a, b, r):
    """Catches a price curve that rewards paying more anywhere in its domain of
    known prices."""
    cheap, dear = sorted((a, b))
    assert score_price(cheap, {"price": r}) >= score_price(dear, {"price": r})


# --------------------------------------------------------------------------
# 4. Percentile rank depends on order, and only on order
# --------------------------------------------------------------------------


@given(a=finite, b=finite, pop=population)
def test_percentile_preserves_order(a, b, pop):
    """Catches a normalization that ranks a larger value below a smaller one."""
    lo, hi = sorted((a, b))
    assert percentile_normalize(lo, pop) <= percentile_normalize(hi, pop)


# None of these can overflow on a finite float. The first version used x ** 3
# and 3x + 7, and Hypothesis found `(5.6e102) ** 3` raising OverflowError -- a
# bug in the TEST's transform, not the engine.
_TRANSFORMS = {
    "affine": lambda x: x / 3.0 - 7.0,
    "cbrt": math.cbrt,
    "atan": math.atan,
}


@given(v=finite, pop=population, name=st.sampled_from(sorted(_TRANSFORMS)))
def test_percentile_is_invariant_under_increasing_transforms(v, pop, name):
    """Catches a normalization that reads magnitudes, not ranks -- min-max
    sneaking back in (ADR-0011). Rescaling the catalog and the value together
    must not move any score."""
    g = _TRANSFORMS[name]
    # "Strictly increasing" has to hold IN FLOATS on this sample, or g is not
    # the transform the property is about. Hypothesis found the gap on the
    # unfiltered version -- shrunk to affine 3x+7, v=4.27e-210, pop=[0.0]: 3v+7
    # and 3*0+7 both round to 7.0, so a strict < became a tie and the rank moved
    # from 100 to 50. That is float rounding manufacturing a tie, and the
    # engine scoring a tie as a tie is correct.
    for p in pop:
        assume((v < p) == (g(v) < g(p)) and (v > p) == (g(v) > g(p)))
    assert percentile_normalize(g(v), [g(p) for p in pop]) == percentile_normalize(v, pop)


# --------------------------------------------------------------------------
# 5. normalize() is idempotent
# --------------------------------------------------------------------------

_cjk = st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF)
_emoji = st.characters(min_codepoint=0x1F300, max_codepoint=0x1FAFF)
_control = st.characters(categories=["Cc", "Cf"])
_any = st.characters(exclude_categories=["Cs"])


# Random characters alone almost never spell "(10-core)", so the first version
# of this property passed with the core-count rewrite moved after the whitespace
# collapse -- the exact ordering bug normalize()'s own comment warns about.
# Fragments from real part names are mixed in so the steps that match WORDS get
# exercised, not only the ones that map characters.
_FRAGMENTS = st.sampled_from([
    "(10-core)", "10-core", "( 8 core )", "10 core", "Processor", "processor",
    "Apple M5 ", "®", "™", "²", " ", "  ", "	", "(", ")", "-", "‑",
])
_names = st.lists(
    st.one_of(_FRAGMENTS, st.text(alphabet=st.one_of(_any, _cjk, _emoji, _control), max_size=4)),
    max_size=12,
).map("".join)


# The shrunk counter-examples of a bug this property found (a strict xfail
# until the core-count rewrite was fixed), pinned so they are checked on every
# run rather than only when Hypothesis happens to draw them.
@example(s="(10-core))")
@example(s="((10-core)")
@example(s="10-coreProcessor")
@given(s=_names)
def test_normalize_is_idempotent(s):
    """Catches a pipeline step whose output a later step (or the same step)
    would still change -- e.g. a character NFKD decomposes INTO one the
    trademark strip removes, or lower() producing something NFKD would split.
    A non-idempotent key means the same part caches under two keys."""
    once = normalize(s)
    assert normalize(once) == once
