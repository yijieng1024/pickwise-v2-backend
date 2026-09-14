"""
Benchmark resolution against a real benchmark table.

The unit tier pins the string pipeline; the marks themselves need rows, so the
desktop/laptop rewrite can only be measured here. Seeded with the real PassMark
values rather than round numbers — the assertion is about two strings landing on
the same row, and a fabricated table cannot show that.
"""

import pytest

from app.pickscore import benchmark_service as _bench

pytestmark = pytest.mark.integration


# The seven collisions. Every laptop in this catalog is a laptop, so a GPU
# string naming a desktop part is always wrong; Acer's source writes
# "GeForce RTX 5060" where ASUS writes "GeForce RTX 5060 Laptop GPU", and the
# desktop rows scored identical silicon up to 23% apart.
_LAPTOP_MARKS = {
    "GeForce RTX 3050 4GB Laptop GPU": 9503,
    "GeForce RTX 4050 Laptop GPU": 14245,
    "GeForce RTX 5050 Laptop GPU": 14176,
    "GeForce RTX 5060 Laptop GPU": 16785,
    "GeForce RTX 5070 Laptop GPU": 19146,
    "GeForce RTX 5080 Laptop GPU": 26326,
    "GeForce RTX 5090 Laptop GPU": 28248,
}

# The desktop rows that sit next to them in PassMark and that the bare strings
# would otherwise fuzzy-match onto.
_DESKTOP_MARKS = {
    "GeForce RTX 3050": 11217,
    "GeForce RTX 4050": 16104,
    "GeForce RTX 5050": 18008,
    "GeForce RTX 5060": 20722,
    "GeForce RTX 5070": 26095,
    "GeForce RTX 5080": 35098,
    "GeForce RTX 5090": 41282,
    # A Ti that must not be won by its non-Ti base.
    "GeForce RTX 5070 Ti Laptop GPU": 22465,
}


@pytest.fixture
def gpu_benchmarks():
    return list(_LAPTOP_MARKS.items()) + list(_DESKTOP_MARKS.items())


@pytest.fixture(autouse=True)
def clear_benchmark_cache():
    """The module-level cache outlives a test. Clearing it is what keeps these
    independent of ordering — and see the cache-key test at the bottom."""
    _bench._cache.clear()
    yield
    _bench._cache.clear()


@pytest.mark.parametrize(
    "bare,laptop_string",
    [
        ("GeForce RTX 3050", "GeForce RTX 3050 4GB Laptop GPU"),
        ("GeForce RTX 4050", "GeForce RTX 4050 Laptop GPU"),
        ("GeForce RTX 5050", "GeForce RTX 5050 Laptop GPU"),
        ("GeForce RTX 5060", "GeForce RTX 5060 Laptop GPU"),
        ("GeForce RTX 5070", "GeForce RTX 5070 Laptop GPU"),
        ("GeForce RTX 5080", "GeForce RTX 5080 Laptop GPU"),
        ("GeForce RTX 5090", "GeForce RTX 5090 Laptop GPU"),
    ],
)
def test_a_bare_desktop_string_resolves_to_its_laptop_sibling(
    bare, laptop_string, gpu_benchmarks
):
    """
    THE REQUIREMENT: identical silicon must score identically regardless of
    which brand's copywriter named it. Not "the bare string resolves to
    something reasonable" — the same mark, to the integer.
    """
    from_bare = _bench.resolve_gpu_benchmark(bare, "Intel Core i7-14650HX", gpu_benchmarks)
    _bench._cache.clear()
    from_laptop = _bench.resolve_gpu_benchmark(
        laptop_string, "Intel Core i7-14650HX", gpu_benchmarks
    )
    assert from_bare["score"] == from_laptop["score"] == _LAPTOP_MARKS[laptop_string]


def test_the_rewrite_never_reaches_for_the_desktop_mark(gpu_benchmarks):
    """The failure this prevents, stated as a number: a bare RTX 5060 scoring
    20722 instead of 16785 is 23% of free performance on a string convention."""
    result = _bench.resolve_gpu_benchmark(
        "GeForce RTX 5060", "Intel Core i7-14650HX", gpu_benchmarks
    )
    assert result["score"] != _DESKTOP_MARKS["GeForce RTX 5060"]


def test_a_ti_is_not_swallowed_by_its_base(gpu_benchmarks):
    """The rewrite is an exact match after stripping the suffix, never a prefix
    match: 'rtx 5070' must not win 'rtx 5070 ti laptop gpu'."""
    result = _bench.resolve_gpu_benchmark(
        "GeForce RTX 5070", "Intel Core i7-14650HX", gpu_benchmarks
    )
    assert result["score"] == _LAPTOP_MARKS["GeForce RTX 5070 Laptop GPU"]
    assert result["score"] != _DESKTOP_MARKS["GeForce RTX 5070 Ti Laptop GPU"]


# --------------------------------------------------------------------------
# The cache key
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "LIVE BUG, reported not fixed. _cache is keyed on the normalized model "
        "string alone and resolve_benchmark serves both tables, so the second "
        "lookup of a string returns the first lookup's mark for the 5-minute TTL. "
        "Measured against the real tables: 'AMD Ryzen Z1 Extreme' is in BOTH "
        "(cpu_mark 24613, gpu_mark 6428). _score_cpu runs before _score_gpu, so "
        "the GPU would be scored 24613 -- 3.8x its real mark. Not reachable on "
        "today's catalog (zero laptops carry that chip) but one handheld-class "
        "ingest away. Fix is a table discriminator in the key."
    ),
)
def test_the_benchmark_cache_is_keyed_only_on_the_model_string(gpu_benchmarks):
    """
    Known open issue, measured rather than argued.

    `_cache` is keyed on the normalized model string alone, and resolve_benchmark
    serves both CPU and GPU lookups. This feeds a string that exists in BOTH
    tables with different marks and asserts the two results differ — i.e. that
    the collision does not happen. It does happen.
    """
    cpu_table = [("GeForce RTX 5060", 1111)]
    gpu_table = [("GeForce RTX 5060", 16785)]

    first = _bench.resolve_benchmark("GeForce RTX 5060", cpu_table)
    second = _bench.resolve_benchmark("GeForce RTX 5060", gpu_table)

    assert first["score"] != second["score"], (
        "the same string resolved against two different benchmark tables returned "
        "the same mark: the cache is serving a CPU lookup from a GPU lookup"
    )
