"""
The string pipeline in benchmark_service.

This is the file that would have caught the inert `\\bprocessor\\b` strip on day
one instead of day three. The bug was: re.sub ran before .lower() with no
IGNORECASE, so 'Processor' with a capital P was never stripped, and 51 CPU rows
kept resolving wrong while the fix looked applied.

Nothing here touches the database. `_normalize` is a pure string function, which
is exactly why it is the cheapest thing in the project to pin down.
"""

import pytest

from ._adapters import (
    confidence_threshold,
    has_anchor_token,
    laptop_variant,
    normalize,
    strip_laptop_suffix,
    variant_key,
)


# --------------------------------------------------------------------------
# Order of operations
# --------------------------------------------------------------------------
# The pipeline is: strip trademark/superscript/curly-quote -> NFKD -> lower ->
# word strip -> collapse whitespace. Each case below breaks if one step moves.


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Trademark stripped BEFORE NFKD. U+2122 decomposes to the letters "TM",
        # so folding first would produce "rtxtm" here.
        ("RTX\u2122 5060 Laptop GPU", "rtx 5060 laptop gpu"),
        ("Intel\u00ae Core\u2122 i7-14650HX", "intel core i7-14650hx"),
        # "Processor" carries no discriminating information -> stripped.
        # Capital P is the regression case.
        ("Intel Core 5 Processor 210H", "intel core 5 210h"),
        ("Intel Core 5 PROCESSOR 210H", "intel core 5 210h"),
        ("Intel Core 5 processor 210H", "intel core 5 210h"),
        # "Laptop" DOES discriminate (laptop vs desktop silicon) -> kept.
        ("NVIDIA GeForce RTX 5070 Ti Laptop GPU", "nvidia geforce rtx 5070 ti laptop gpu"),
        # " GPU" discriminates too: PassMark lists Arc 140T and Arc 140T GPU as
        # separate parts, 5634 vs 6607, 17% apart.
        ("Intel Arc 140T GPU", "intel arc 140t gpu"),
        ("Intel Arc 140T", "intel arc 140t"),
        # Whitespace collapse happens last.
        ("  AMD   Radeon   890M  ", "amd radeon 890m"),
    ],
)
def test_normalize_pipeline(raw, expected):
    assert normalize(raw) == expected


def test_trademark_never_becomes_letters():
    """The specific NFKD trap, asserted on its own so the failure message is clear."""
    assert "tm" not in normalize("RTX\u2122 5060")


def test_arc_140t_and_arc_140t_gpu_stay_distinct():
    """If these two ever collapse, the 17% mark gap becomes silent."""
    assert normalize("Intel Arc 140T") != normalize("Intel Arc 140T GPU")


def test_normalize_is_idempotent():
    """Running the pipeline twice must not change the result. Catches any step
    that is unsafe to re-apply (a second word strip, a double fold)."""
    for raw in [
        "RTX\u2122 5060 Laptop GPU",
        "Intel Core 5 Processor 210H",
        "Apple M5 Max 40-Core GPU",
    ]:
        once = normalize(raw)
        assert normalize(once) == once


def test_normalize_does_not_crash_on_empty_or_none_like():
    assert normalize("") == ""
    assert normalize("   ") == ""


# --------------------------------------------------------------------------
# Anchor-token gate
# --------------------------------------------------------------------------
# Rule: a string with no token carrying a digit and >=3 chars cannot identify a
# specific part, so it must not be fuzzy-matched at all. Without this gate,
# 'AMD Radeon Graphics' matched a pre-2005 row at 0.855 confidence — above the
# threshold, and no threshold could have separated it, because the failure is
# not a string-similarity failure.


@pytest.mark.parametrize(
    "raw",
    [
        "AMD Radeon Graphics",
        "Intel Graphics",
        "Intel UHD Graphics",
        "GeForce GPU",
        "Unknown",
    ],
)
def test_anchorless_strings_are_gated(raw):
    assert has_anchor_token(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        "NVIDIA GeForce RTX 5060 Laptop GPU",
        "Intel Arc 140T GPU",
        "AMD Radeon 890M",
        "Intel Core i7-14650HX",
    ],
)
def test_real_part_names_have_an_anchor(raw):
    assert has_anchor_token(raw) is True


def test_apple_core_count_strings_are_gated():
    """
    This case pins down the tokenizer rule, and it is worth reading the failure
    carefully rather than just making it green.

    Apple's GPU strings look like '10-core GPU'. If tokens are split on
    whitespace, '10-core' has a digit and 7 chars, so it would PASS the gate and
    reach the fuzzy matcher — which is the behaviour that produced the 0.855
    mismatch in the first place. If tokens are split on non-alphanumeric, the
    pieces are '10' (2 chars) and 'core' (no digit), so it correctly fails.

    So: if this test goes red, the tokenizer is splitting on whitespace and that
    is a real finding, not a bad test.
    """
    assert has_anchor_token("10-core GPU") is False
    assert has_anchor_token("40-Core GPU") is False


def test_confidence_threshold_is_the_tightened_value():
    """Raised 0.6 -> 0.85 after the four mismatches. A silent revert would
    reopen the anchorless-match class."""
    assert confidence_threshold() == pytest.approx(0.85)


# --------------------------------------------------------------------------
# Desktop/laptop rewrite — the string half
# --------------------------------------------------------------------------
# The mark values (RTX 5060 20722 -> 16785 and friends) need the benchmark
# table, so they live in the integration tier. What is unit-testable is the
# suffix rule, and specifically that it is an EXACT match after stripping,
# never a prefix match.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("rtx 5070 laptop gpu", "rtx 5070"),
        ("rtx 5070 ti laptop gpu", "rtx 5070 ti"),
        ("rtx 5090 laptop gpu", "rtx 5090"),
        # No suffix -> unchanged. An Acer row already names the desktop part.
        ("rtx 5060", "rtx 5060"),
    ],
)
def test_strip_laptop_suffix(raw, expected):
    assert strip_laptop_suffix(raw) == expected


def test_ti_variant_is_not_swallowed_by_its_base():
    """
    'rtx 5070' must not win 'rtx 5070 ti laptop gpu'. This is why the rewrite is
    an exact match on the stripped name rather than a startswith() — a prefix
    rule would silently give every Ti card its non-Ti mark.
    """
    assert strip_laptop_suffix("rtx 5070 ti laptop gpu") != "rtx 5070"


# --------------------------------------------------------------------------
# Vendor prefixes must not defeat the desktop/laptop rewrite
# --------------------------------------------------------------------------
# _laptop_variant compared the whole normalized string against the
# suffix-stripped row name, so "nvidia geforce rtx 4050" never equalled
# "geforce rtx 4050" and the rewrite silently did not fire. The string then
# fell through to the fuzzy matcher and landed on "RTX PRO 2000 Blackwell
# Embedded GPU" -- not the desktop variant of the right part, an unrelated
# one. Worse than the seven collisions the rewrite was built for.
#
# The rule is ADR-0010's: drop words carrying no discriminating information,
# keep words that do. NVIDIA/AMD/Intel name a vendor and nothing else. GeForce,
# Radeon and Arc name product families and are kept, as are "Laptop" and the
# trailing " GPU" -- Intel Arc 140T and Intel Arc 140T GPU are different parts,
# 17% apart.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nvidia geforce rtx 4050", "geforce rtx 4050"),
        ("geforce rtx 4050", "geforce rtx 4050"),
        ("amd radeon rx 7600s", "radeon rx 7600s"),
        ("intel arc 140t gpu", "arc 140t gpu"),
        # The discriminating words survive.
        ("geforce rtx 5070 ti laptop gpu", "geforce rtx 5070 ti laptop gpu"),
    ],
)
def test_variant_key_drops_only_the_vendor(raw, expected):
    assert variant_key(raw) == expected


def test_variant_key_keeps_the_gpu_suffix():
    """PassMark lists Arc 140T and Arc 140T GPU separately, 5634 vs 6607.
    Dropping the trailing GPU here would merge two real parts."""
    assert variant_key("intel arc 140t") != variant_key("intel arc 140t gpu")


_REWRITE_TABLE = [
    ("GeForce RTX 3050 4GB Laptop GPU", 9503),
    ("GeForce RTX 4050 Laptop GPU", 14245),
    ("GeForce RTX 5050 Laptop GPU", 14176),
    ("GeForce RTX 5060 Laptop GPU", 16785),
    ("GeForce RTX 5070 Laptop GPU", 19146),
    ("GeForce RTX 5080 Laptop GPU", 26326),
    ("GeForce RTX 5090 Laptop GPU", 28248),
    ("GeForce RTX 5070 Ti Laptop GPU", 22465),
    # The desktop rows a bare string would otherwise fuzzy-match onto.
    ("GeForce RTX 4050", 16104),
    ("GeForce RTX 5060", 20722),
    ("RTX PRO 2000 Blackwell Embedded GPU", 16242),
]


@pytest.mark.parametrize(
    "bare,laptop_row",
    [
        ("nvidia geforce rtx 4050", "GeForce RTX 4050 Laptop GPU"),
        ("geforce rtx 4050", "GeForce RTX 4050 Laptop GPU"),
        ("nvidia geforce rtx 5060", "GeForce RTX 5060 Laptop GPU"),
        ("geforce rtx 5060", "GeForce RTX 5060 Laptop GPU"),
    ],
)
def test_the_rewrite_fires_with_or_without_the_vendor_prefix(bare, laptop_row):
    """The regression test: prefixed and bare forms must reach the same row."""
    assert laptop_variant(bare, _REWRITE_TABLE) == laptop_row


def test_a_ti_is_still_not_won_by_its_base_after_the_change():
    """
    The comparison stays EXACT on the canonical form. Loosening it to a prefix
    or substring match is how "rtx 5070" would start winning "rtx 5070 ti
    laptop gpu".

    Asserted against a table holding ONLY the Ti row, because with both present
    the base name correctly resolves to its own laptop row -- which proves
    nothing about the Ti.
    """
    ti_only = [("GeForce RTX 5070 Ti Laptop GPU", 22465)]
    assert laptop_variant("nvidia geforce rtx 5070", ti_only) is None
    assert laptop_variant("geforce rtx 5070", ti_only) is None
    # And with both rows present, the base picks its own -- never the Ti.
    assert laptop_variant("geforce rtx 5070", _REWRITE_TABLE) == "GeForce RTX 5070 Laptop GPU"


def test_the_variant_override_still_fires_with_a_prefix():
    """_GPU_VARIANT_OVERRIDES is keyed on the bare normalized string. A
    prefixed string has to reach it too, or the 4GB pin is bypassed."""
    assert laptop_variant("geforce rtx 3050", _REWRITE_TABLE) == "GeForce RTX 3050 4GB Laptop GPU"
    assert laptop_variant("nvidia geforce rtx 3050", _REWRITE_TABLE) == "GeForce RTX 3050 4GB Laptop GPU"
