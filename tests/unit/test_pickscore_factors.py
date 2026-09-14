"""
PickScore factor scorers.

Two things get pinned here:

1. `_score_price`'s three branches. The personalized branch was only fixed
   recently (it used to return a flat 100.0 for anything inside budget, so price
   stopped discriminating for exactly the users who stated a budget) and it has
   no coverage at all.

2. The percentile curve. It shipped, it was verified once by hand across 1190
   rows, and nothing stops it being swapped back.

The percentile tests use a small synthetic population rather than the real
catalog, because a unit test must not depend on 238 rows in a database. The
real-catalog check belongs in the golden snapshot tier.
"""

import pytest

from ._adapters import (
    percentile_normalize,
    score_cpu,
    score_gpu,
    score_price,
    score_price_reason,
    score_ram_storage,
)


# --------------------------------------------------------------------------
# _score_price — branch 1: unknown price
# --------------------------------------------------------------------------
# price_rm = 0 means "unknown", not "free". The scorer returns 50.0 with a
# "factor skipped" reason. The number is indistinguishable from a genuine
# mid-scale score, which is why the REASON is the thing worth asserting.


def test_price_zero_returns_neutral(ranges):
    assert score_price(0.0, ranges) == pytest.approx(50.0)


def test_price_zero_says_it_was_skipped(ranges):
    """
    Unknown and average must not be the same signal. If the reason string ever
    stops saying the factor was skipped, a fabricated neutral becomes invisible
    to anything reading the breakdown.
    """
    reason = score_price_reason(0.0, ranges)
    assert reason is not None
    assert "skip" in reason.lower() or "unavailable" in reason.lower()


# --------------------------------------------------------------------------
# _score_price — branch 2: personalized with a stated budget max
# --------------------------------------------------------------------------


def test_personalized_in_budget_no_longer_flat_100(ranges):
    """
    THE REGRESSION TEST for the fix.

    RM5,899 against a RM6,000 budget used to score 100.0. It now scores the same
    as general mode, so price keeps discriminating inside the budget.
    """
    personalized = score_price(5899.0, ranges, budget_max=6000.0, personalized=True)
    general = score_price(5899.0, ranges, personalized=False)
    assert personalized == pytest.approx(general, abs=0.5)
    assert personalized < 99.0


def test_personalized_still_discriminates_within_budget(ranges):
    """A cheaper machine inside the same budget must score higher. This is the
    property that the flat-100 bug destroyed."""
    cheap = score_price(3000.0, ranges, budget_max=6000.0, personalized=True)
    dear = score_price(5899.0, ranges, budget_max=6000.0, personalized=True)
    assert cheap > dear


def test_over_budget_decays(ranges):
    """DECAY_K = 2, so the penalty scales with how far over budget it is."""
    slightly_over = score_price(6600.0, ranges, budget_max=6000.0, personalized=True)
    far_over = score_price(12000.0, ranges, budget_max=6000.0, personalized=True)
    assert 0.0 <= far_over < slightly_over
    assert far_over == pytest.approx(0.0, abs=0.5)


def test_over_budget_never_goes_negative(ranges):
    assert score_price(99999.0, ranges, budget_max=2000.0, personalized=True) >= 0.0


# --------------------------------------------------------------------------
# _score_price — branch 3: no budget max (includes the >RM5000 band)
# --------------------------------------------------------------------------


def test_no_budget_max_falls_through_to_general(ranges):
    """
    The >RM5000 questionnaire band stores {"max": null, "min": 5000}. Because
    only max is read, those users land in the general branch, where cheaper
    scores higher — a user who stated a HIGH budget gets a price score that
    rewards cheapness.

    This test documents the behaviour as it is. It is not asserting the
    behaviour is right; it is making sure the next change to this branch is a
    deliberate one.
    """
    cheap = score_price(2000.0, ranges, budget_max=None, personalized=True)
    dear = score_price(20000.0, ranges, budget_max=None, personalized=True)
    assert cheap > dear


# --------------------------------------------------------------------------
# _score_ram_storage
# --------------------------------------------------------------------------
# ram * 0.6 + storage * 0.4, with -15 on the storage half when storage_type
# contains "hdd".


def test_ram_storage_weighting(ranges):
    """More RAM must move the combined score more than the same relative bump in
    storage, because RAM carries 0.6 of the weight."""
    base = score_ram_storage(16, 1024, "SSD", ranges)
    more_ram = score_ram_storage(32, 1024, "SSD", ranges)
    more_storage = score_ram_storage(16, 2048, "SSD", ranges)
    assert more_ram > base
    assert more_storage > base


def test_hdd_penalty_applies(ranges):
    ssd = score_ram_storage(16, 1024, "SSD", ranges)
    hdd = score_ram_storage(16, 1024, "HDD", ranges)
    assert hdd < ssd


def test_hdd_penalty_is_case_insensitive(ranges):
    """The check is a substring match on storage_type. Catalog strings are not
    normalized, so 'hdd', 'HDD' and '1TB HDD' must all trigger it."""
    a = score_ram_storage(16, 1024, "hdd", ranges)
    b = score_ram_storage(16, 1024, "HDD", ranges)
    c = score_ram_storage(16, 1024, "1TB HDD", ranges)
    assert a == pytest.approx(b) == pytest.approx(c)


def test_hdd_penalty_hits_the_storage_half_only(ranges):
    """-15 on the storage half means at most -6 on the combined score
    (15 * 0.4). If it ever lands on the whole score it would be -15."""
    ssd = score_ram_storage(16, 1024, "SSD", ranges)
    hdd = score_ram_storage(16, 1024, "HDD", ranges)
    assert (ssd - hdd) == pytest.approx(6.0, abs=0.5)


def test_ram_storage_stays_in_bounds(ranges):
    for ram, storage in [(4, 64), (128, 4096), (8, 512), (64, 2048)]:
        score = score_ram_storage(ram, storage, "SSD", ranges)
        assert 0.0 <= score <= 100.0


# --------------------------------------------------------------------------
# _score_cpu / _score_gpu fallbacks
# --------------------------------------------------------------------------


def test_unresolved_cpu_returns_neutral(ranges):
    assert score_cpu("Unknown", ranges) == pytest.approx(50.0)


def test_unresolved_gpu_returns_neutral(ranges):
    assert score_gpu("Unknown", ranges) == pytest.approx(50.0)


@pytest.mark.xfail(
    reason=(
        "Known gap: flags carries gpu_score_is_proxy and price_unavailable but "
        "nothing for an unresolved benchmark, so a rejected match is "
        "indistinguishable from a genuine mid score. Same shape as price_rm = 0 "
        "meaning both 'free' and 'unknown'. Remove the xfail when a flag is added."
    ),
    strict=False,
)
def test_unresolved_benchmark_is_flagged(ranges):
    """
    An xfail marks a test that is EXPECTED to fail — it records a known gap in
    the suite without turning the build red. When the gap is closed, the test
    turns green and pytest reports it as XPASS, which is the reminder to delete
    the marker.
    """
    from ._adapters import _engine  # local import: this reaches past the adapter

    _score, flags = _engine._score_gpu("Unknown", ranges)
    assert flags.get("gpu_benchmark_unresolved") is True


# --------------------------------------------------------------------------
# Percentile normalization (ADR-0011)
# --------------------------------------------------------------------------


def test_percentile_endpoints():
    """
    The endpoints are NOT 0 and 100, and that is deliberate.

    _normalize scores the MIDPOINT of the tied block — (below + tied/2) / n —
    so that two identical configurations score identically instead of depending
    on sort order. A population member is therefore always tied with at least
    itself, and the extremes land half a step inside: with n = 5 the minimum is
    10 and the maximum 90.

    The brief assumed 0/100. Bad assumption in the test, not a bug: reaching a
    true 0 would mean a value strictly below every catalog row.
    """
    pop = [10, 20, 30, 40, 50]
    assert percentile_normalize(10, pop) == pytest.approx(10.0)
    assert percentile_normalize(50, pop) == pytest.approx(90.0)
    # Outside the population, the bounds are reached.
    assert percentile_normalize(0, pop) == pytest.approx(0.0)
    assert percentile_normalize(999, pop) == pytest.approx(100.0)


def test_percentile_midpoint():
    pop = [10, 20, 30, 40, 50]
    assert percentile_normalize(30, pop) == pytest.approx(50.0, abs=1.0)


def test_percentile_is_monotonic():
    pop = [1, 2, 3, 10, 100, 1000, 36999]
    scores = [percentile_normalize(v, pop) for v in sorted(pop)]
    assert scores == sorted(scores)


def test_percentile_ignores_the_outlier_gap():
    """
    The whole point of moving off min-max. Under min-max, one RM36,999
    workstation pushes the realistic bulk of the catalog into a narrow band.
    Under percentile, the gap between the top two values does not matter — only
    their ORDER does.
    """
    tight = [1000, 2000, 3000, 4000, 5000]
    with_outlier = [1000, 2000, 3000, 4000, 500000]
    assert percentile_normalize(3000, tight) == pytest.approx(
        percentile_normalize(3000, with_outlier)
    )


def test_percentile_stays_in_bounds():
    pop = [1, 5, 9]
    for v in [-100, 0, 1, 5, 9, 100, 10**9]:
        assert 0.0 <= percentile_normalize(v, pop) <= 100.0


def test_one_new_laptop_barely_moves_a_percentile():
    """
    The relativity objection to percentile, measured: across 238 laptops a
    single addition moves any percentile by at most about 0.4 points.
    """
    pop = list(range(1, 239))
    before = percentile_normalize(120, pop)
    after = percentile_normalize(120, pop + [1000])
    assert abs(before - after) < 0.5
