"""
Qualcomm part numbers (benchmark_service._QUALCOMM_PART and the exact
part-number path in resolve_benchmark).

Before this, every Snapdragon X laptop but one scored a flagged neutral CPU 50:
the catalog writes "X1 26 100" / "X1-26-100", PassMark writes "X126100" or
"X1E-78-100", and fuzzy matching against PassMark's long " - Qualcomm Oryon"
names tops out at 0.855, under the 0.90 CPU threshold.
"""

import pytest

from ._adapters import integrated_lookup, normalize, resolve_cpu

# Real PassMark names (cpu_benchmarks, 2026-10-25), plus the decoys the fuzzy
# matcher used to tie with at 0.855.
CPU_TABLE = [
    ("Snapdragon X - X126100 - Qualcomm Oryon", 16423),
    ("Snapdragon X Plus - X1P42100 - Qualcomm Oryon", 17905),
    ("Qualcomm Snapdragon X Plus X1P-64-100", 21387),
    ("Qualcomm Snapdragon X Elite - X1E-78-100", 22695),      # PassMark id 6095
    ("Snapdragon X Elite - X1E78100 - Qualcomm Or", 20376),   # PassMark id 7089
    ("Qualcomm Snapdragon X Elite - X1E-80-100", 22875),
    ("Snapdragon X2 Elite - X2E88100 - Qualcomm Oryo", 37950),
    ("Snapdragon X2 Elite Extreme - X2E94100 - Qualc", 44556),
    ("Cobalt 100", 8134),
    ("Snapdragon 435", 1080),
    ("AMD Athlon 64 X2 4200+", 767),
]


# --------------------------------------------------------------------------
# The regex joins the four spellings, and only those
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Snapdragon X X1 26 100 Processor", "snapdragon x x126100"),
        ("Snapdragon® X X1 26 100 Processor", "snapdragon x x126100"),
        ("Snapdragon X X1-26-100", "snapdragon x x126100"),
        ("Snapdragon X Plus X1P 42 100 Processor", "snapdragon x plus x1p42100"),
        ("Snapdragon® X Elite X1E 78 100 Processor", "snapdragon x elite x1e78100"),
        ("Qualcomm Snapdragon X Elite - X1E-78-100", "qualcomm snapdragon x elite - x1e78100"),
        ("Snapdragon X Elite - X1E78100 - Qualcomm Or", "snapdragon x elite - x1e78100 - qualcomm or"),
        ("Snapdragon X2 Elite (18-core) X2E88100", "snapdragon x2 elite 18 core x2e88100"),
    ],
)
def test_part_number_spellings_join(raw, expected):
    assert normalize(raw) == expected
    assert normalize(normalize(raw)) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # "X2 4200" is 4 digits, not 2+3.
        ("AMD Athlon 64 X2 4200+", "amd athlon 64 x2 4200+"),
        # Adreno GPU names carry X1-<2 digits> with no 3-digit group.
        ("Adreno X1-45", "adreno x1-45"),
        ("Adreno X1-85", "adreno x1-85"),
        ("Qualcomm Adreno X1-45 GPU", "qualcomm adreno x1-45 gpu"),
        ("ThinkPad X1 Carbon", "thinkpad x1 carbon"),
        ("Snapdragon 435", "snapdragon 435"),
        ("Qualcomm Snapdragon X Elite - X1E001DE", "qualcomm snapdragon x elite - x1e001de"),
    ],
)
def test_non_part_numbers_untouched(raw, expected):
    assert normalize(raw) == expected


# --------------------------------------------------------------------------
# Exact part-number resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "catalog,expected_row",
    [
        ("Snapdragon X X1 26 100 Processor", "Snapdragon X - X126100 - Qualcomm Oryon"),
        ("Snapdragon® X X1 26 100 Processor", "Snapdragon X - X126100 - Qualcomm Oryon"),
        ("Snapdragon X X1-26-100", "Snapdragon X - X126100 - Qualcomm Oryon"),
        ("Snapdragon X Plus X1P 42 100 Processor", "Snapdragon X Plus - X1P42100 - Qualcomm Oryon"),
        ("Snapdragon® X Elite X1E 78 100 Processor", "Qualcomm Snapdragon X Elite - X1E-78-100"),
        ("Snapdragon X2 Elite (18-core) X2E88100", "Snapdragon X2 Elite - X2E88100 - Qualcomm Oryo"),
        ("Snapdragon® X2 Elite (18-core) X2E88100", "Snapdragon X2 Elite - X2E88100 - Qualcomm Oryo"),
        # Already resolved by fuzzy at 0.95; the exact path must agree.
        ("Snapdragon X2 Elite Extreme X2E94100", "Snapdragon X2 Elite Extreme - X2E94100 - Qualc"),
    ],
)
def test_every_catalog_snapdragon_string_resolves(catalog, expected_row):
    result = resolve_cpu(catalog, CPU_TABLE)
    assert result["matched_name"] == expected_row
    assert result["score"] == dict(CPU_TABLE)[expected_row]


@pytest.mark.parametrize(
    "catalog",
    ["Snapdragon X Elite X1E-78-100", "Snapdragon X Elite X1E 78 100", "Snapdragon X Elite X1E78100"],
)
@pytest.mark.parametrize("table", [CPU_TABLE, CPU_TABLE[::-1]], ids=["db-order", "reversed"])
def test_x1e78100_picks_the_curated_passmark_row(catalog, table):
    """Two PassMark rows carry X1E78100. 6095 is the curated entry (TDP,
    first-seen date, 288 samples, lists the Windows name string as an alias);
    7089 is a row created from a truncated name string. Row order must not
    decide it."""
    result = resolve_cpu(catalog, table)
    assert result["matched_name"] == "Qualcomm Snapdragon X Elite - X1E-78-100"
    assert result["score"] == 22695


def test_unknown_part_number_falls_back_to_fuzzy_and_stays_unresolved():
    """A part number with no row in the table must not borrow a neighbour's."""
    result = resolve_cpu("Snapdragon X Elite X1E 99 100", CPU_TABLE)
    assert result["score"] is None


# --------------------------------------------------------------------------
# _INTEGRATED_GPU_BY_CPU: normalized keys and the 2026-10-25 HP additions
# --------------------------------------------------------------------------


def test_hyphenated_snapdragon_finds_the_spaced_key():
    """HP writes X1-26-100; the map key is 'snapdragon x x1 26 100'."""
    assert integrated_lookup("Snapdragon X X1-26-100") == "Qualcomm Adreno X1-45 GPU"
    assert integrated_lookup("Snapdragon X X1 26 100 Processor") == "Qualcomm Adreno X1-45 GPU"


@pytest.mark.parametrize(
    "cpu,expected_gpu",
    [
        ("AMD Athlon™ Silver 7120U", "Radeon 610M"),
        ("AMD Ryzen 5 7535HS", "Radeon 660M"),
        ("Intel Core 3 100U", "Intel Graphics"),
        ("Intel® Core™ Ultra 5 322", "Intel Graphics"),
        ("Intel Core Ultra 5 322", "Intel Graphics"),
        ("Intel® Core™ Ultra 7 356H", "Intel Graphics"),
        ("AMD Ryzen AI 5 435", "Radeon 840M"),
        ("AMD Ryzen 5 PRO 230", "Radeon 760M"),
        ("AMD Ryzen AI 7 PRO 450", "Radeon 860M"),
        ("AMD Ryzen AI Max PRO 385", "Radeon 8050S"),
    ],
)
def test_hp_additions(cpu, expected_gpu):
    assert integrated_lookup(cpu) == expected_gpu


@pytest.mark.parametrize(
    "cpu,expected_gpu",
    [
        # Neighbours of the new keys keep their own entries.
        ("AMD Ryzen AI 5 430", "Radeon 840M"),
        ("AMD Ryzen AI 7 350", "Radeon 860M"),
        ("AMD Ryzen AI Max+ 395", "Radeon 8060S"),
        ("Intel Core Ultra 5 325", "Intel Graphics"),
        ("AMD Ryzen 5 220", "Radeon 740M"),
    ],
)
def test_existing_neighbours_unchanged(cpu, expected_gpu):
    assert integrated_lookup(cpu) == expected_gpu
