"""
The PickScore golden snapshot.

A golden snapshot does NOT assert the scores are correct. It asserts that a
change to them was DELIBERATE. Every number here was produced by the engine as
it stands; the test's only job is to make the next change to those numbers
visible and reviewable instead of silent.

THESE ARE NOT PRODUCTION SCORES. _normalize ranks by percentile against the
population it is handed, and this population is the 20 fixture laptops, not the
~238 in the catalog. ADR-0011 quotes the F16 at 57/59/62/61/56 against the full
catalog; the F16's numbers here are its rank among these 20 and will not match.
Do not read a production answer out of this file.

Tier 2 tests the SCORING LOGIC, not benchmark resolution: the marks are frozen
alongside the fixture, so a PassMark re-scrape cannot move the golden file.
Resolution has its own coverage in Tier 0 and Tier 3.

Regenerate deliberately, never automatically:

    pytest tests/test_golden_pickscore.py --update-golden
"""

import json

import pytest

from tests.golden.engine_harness import (
    GOLDEN_PATH,
    compute_snapshot,
    fixture_ranges,
    flatten,
    load_fixture,
)

pytestmark = pytest.mark.unit

# The six factors _normalize actually ranks. screen_size and brand are excluded:
# in general mode they are fixed constants (50 and 50), so their spread is zero
# by construction and including them would make the ratio infinite.
_NORMALIZED_FACTORS = ["price", "cpu", "gpu", "ram_storage", "portability", "battery"]


@pytest.fixture(scope="module")
def fixture():
    return load_fixture()


@pytest.fixture(scope="module")
def snapshot(fixture):
    return compute_snapshot(fixture)


# --------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------


def _format_diff(old: dict, new: dict, limit: int = 30) -> str:
    """
    A table, not a dict diff.

    The question this has to answer in five seconds is: did ONE FACTOR move
    everywhere, or did EVERYTHING move on one laptop? Those are different bugs
    -- a weight profile edit versus a fixture or resolution change -- and a
    1000-line structural diff answers neither, so it gets skipped instead of
    read.
    """
    rows = []
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        try:
            delta = float(after) - float(before)
        except (TypeError, ValueError):
            delta = None  # a flag flipped, or a mark became null
        rows.append((path, before, after, delta))

    rows.sort(key=lambda r: (r[3] is None, -abs(r[3]) if r[3] is not None else 0))

    lines = [
        f"{len(rows)} value(s) changed.",
        "",
        f"{'laptop':16} {'use case':14} {'factor':22} {'old':>10} {'new':>10} {'delta':>9}",
        "-" * 86,
    ]
    for (laptop, use_case, factor), before, after, delta in rows[:limit]:
        shown = f"{delta:+9.2f}" if delta is not None else "        -"
        lines.append(
            f"{laptop:16} {use_case:14} {factor:22} {str(before):>10} {str(after):>10} {shown}"
        )
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more changed value(s) not shown.")

    laptops = {r[0][0] for r in rows}
    factors = {r[0][2] for r in rows}
    lines += [
        "",
        f"spread: {len(laptops)} laptop(s), {len(factors)} distinct factor(s).",
        "  one factor across many laptops -> a weighting or normalization change.",
        "  many factors on one laptop     -> that row's inputs or its resolved marks.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------


def test_scores_match_the_golden_file(snapshot, request):
    """
    Catches an unintended scoring change of any size -- including one too small
    to look wrong in any single number, which is exactly the kind that a weight
    tweak or a normalization edit produces.
    """
    if request.config.getoption("--update-golden"):
        pytest.skip("--update-golden was passed; see test_regenerate_golden")

    assert GOLDEN_PATH.exists(), (
        f"{GOLDEN_PATH} is missing. Create it deliberately:\n"
        "    pytest tests/test_golden_pickscore.py --update-golden"
    )
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["snapshot"]

    if snapshot != expected:
        pytest.fail(_format_diff(flatten(expected), flatten(snapshot)), pytrace=False)


def test_every_fixture_laptop_is_in_the_snapshot(snapshot, fixture):
    """A row silently dropped from the fixture would take its code path with
    it, and the snapshot would still pass."""
    assert set(snapshot) == {row["key"] for row in fixture["laptops"]}
    assert len(snapshot) == 20


def test_the_suspended_row_is_scored_but_not_in_any_range(snapshot, fixture):
    """
    Scoring and recommendability are different questions (ADR-0009): a
    suspended laptop still has a score, it just must not shape the curve every
    other laptop is ranked against.
    """
    assert snapshot["helios16"]["use_cases"]["gaming"]["score"] > 0

    ranges = fixture_ranges(fixture)
    assert 10499.0 not in ranges["price"]["values"], "the suspended price entered the curve"
    assert len(ranges["price"]["values"]) == 18, "20 rows minus one suspended, minus one unpriced"
    # Every other factor drops it too, not just price.
    assert len(ranges["weight_kg"]["values"]) == 19, "20 rows minus one suspended"


def test_the_unpriced_row_is_flagged_and_skipped(snapshot):
    """price_rm = 0 means unknown, not free. It must not drag the price floor
    down, and the flag is the only thing that says the 50 is a skip."""
    entry = snapshot["vivobook15"]["use_cases"]["general_use"]
    assert entry["flags"]["price_unavailable"] is True
    assert entry["factors"]["price"]["raw_score"] == pytest.approx(50.0)
    assert "skip" in (entry["factors"]["price"]["note"] or "").lower()


def test_proxy_gpus_are_flagged(snapshot):
    """Apple, integrated, and unresolved all set the same flag -- the number
    alone cannot tell you a GPU score was not measured."""
    for key in ("mbp16_m5max", "zenbook14", "aspire3", "chromebook"):
        assert snapshot[key]["use_cases"]["gaming"]["flags"]["gpu_score_is_proxy"] is True
    for key in ("f16", "scar18", "omen16"):
        assert snapshot[key]["use_cases"]["gaming"]["flags"]["gpu_score_is_proxy"] is False


def test_resolved_marks_are_recorded(snapshot):
    """
    The workaround for the open xfail: _score_gpu has no flag for an unresolved
    benchmark, so a rejected match and a genuine mid score are the same 50 in
    the breakdown. Recording the mark means a diff shows `gpu_mark: 6607 ->
    null` even when no flag does. The flag is still the real fix.
    """
    assert snapshot["chromebook"]["cpu_mark"] is None
    assert snapshot["chromebook"]["gpu_mark"] is None
    assert snapshot["f16"]["gpu_mark"] == 16704
    assert snapshot["victus15"]["gpu_mark"] == 9505  # the 4GB variant pin, not the generic row


# --------------------------------------------------------------------------
# The effective spread (the half that catches a reintroduced min-max curve)
# --------------------------------------------------------------------------


def _p10_p90_spreads(snapshot) -> dict:
    spreads = {}
    for factor in _NORMALIZED_FACTORS:
        # office_study is arbitrary: raw factor scores are identical across use
        # cases, only the weights differ.
        values = sorted(
            entry["use_cases"]["office_study"]["factors"][factor]["raw_score"]
            for entry in snapshot.values()
        )
        n = len(values)
        p10 = values[int(0.10 * (n - 1))]
        p90 = values[int(0.90 * (n - 1))]
        spreads[factor] = p90 - p10
    return spreads


def test_no_factor_has_a_wildly_different_scale(snapshot):
    """
    THE ASSERTION THE WHOLE NORMALIZATION WORK EXISTS TO PROTECT (ADR-0011).

    Under min-max each factor's scale came from exactly two rows, and for
    price, capacity and weight those two rows are outliers -- price spread 34
    points across the catalog against gpu's 77, so the presets were weighting
    eight factors as though they shared a scale, and a gaming laptop scored
    highest on Office & Study. Under percentile the real catalog measures
    1.57x widest-to-narrowest; under the old min-max it was 3.6x.

    This catches a min-max curve quietly reintroduced for ONE factor -- a
    change that would barely move any individual score enough to look wrong,
    but breaks the property. The per-laptop snapshot above would catch it too;
    this says WHY it matters in one number.

    THE THRESHOLD IS 1.4, NOT THE CATALOG'S 2.0, AND THAT IS THE POINT.

    Measured on this fixture: 1.118x normally, and 1.72x with _normalize
    flipped back to min-max for price alone (price's p10-p90 collapses 77.78 ->
    47.84 because the RM36,999 row then sets the scale by itself). 1.72 is
    UNDER 2.0 -- so the catalog's own bound, applied to 20 rows, does not catch
    the exact regression this assertion exists for. Verified by mutation, not
    assumed.

    2.0 is the right number for the 238-row catalog, where min-max measured
    3.6x. It is the wrong number here, and keeping it because it is the
    familiar one would have left a test that reads like protection and catches
    nothing. 1.4 sits 25% above this fixture's baseline and 19% below its
    worst measured mutation.
    """
    spreads = _p10_p90_spreads(snapshot)
    ratio = max(spreads.values()) / min(spreads.values())
    assert ratio < 1.4, (
        f"widest/narrowest factor spread is {ratio:.2f}x "
        f"(limit 1.4, calibrated to this 20-row fixture)\n"
        + "\n".join(f"  {f:12} p10-p90 = {s:6.2f}" for f, s in sorted(spreads.items()))
    )


def test_the_fixture_spread_is_reported(snapshot):
    """
    Not really an assertion -- a record of what this sample measures, so the
    threshold above is justified by a number rather than by the catalog's.

    Measured 2026-09-14 across the 20 fixture laptops:

        gpu          p10  8.82  p90 91.18  spread 82.36
        portability  p10  7.89  p90 86.84  spread 78.95
        price        p10  8.33  p90 86.11  spread 77.78
        cpu          p10  8.33  p90 86.11  spread 77.78
        ram_storage  p10  7.37  p90 81.58  spread 74.21
        battery      p10  7.89  p90 81.58  spread 73.69
        widest/narrowest = 1.118x

    Tighter than the catalog's 1.57x, and that is an artefact rather than an
    improvement: with 20 rows and percentile ranking the values are nearly
    evenly spaced by construction, so every factor's spread approaches the same
    number whatever the underlying data does. The ratio here is therefore a
    weaker signal than the catalog's, and a threshold set at 1.2 would be
    measuring the sample size rather than the curve.

    n=20 also puts p10 and p90 on the 2nd and 18th values, so each endpoint is
    ONE laptop and a single fixture edit moves it. Widening the fixture would
    make the ratio a stronger signal and let the bound move back toward the
    catalog's 2.0; at 20 rows the bound has to be calibrated against measured
    mutations instead, which is what 1.4 is.
    """
    spreads = _p10_p90_spreads(snapshot)
    assert all(s > 0 for s in spreads.values()), (
        "a factor has zero spread across the fixture, so the fixture does not "
        f"discriminate on it: { {f: s for f, s in spreads.items() if s == 0} }"
    )


# --------------------------------------------------------------------------
# Regeneration
# --------------------------------------------------------------------------


def test_regenerate_golden(snapshot, request):
    """Only does anything under --update-golden, and prints what is being
    accepted before writing it."""
    if not request.config.getoption("--update-golden"):
        pytest.skip("regeneration is deliberate: pass --update-golden")

    if GOLDEN_PATH.exists():
        old = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["snapshot"]
        if old == snapshot:
            print("\ngolden file already matches; nothing to write.")
            return
        print("\nACCEPTING THESE CHANGES:\n")
        print(_format_diff(flatten(old), flatten(snapshot)))
    else:
        print(f"\ncreating {GOLDEN_PATH.name} for the first time.")

    GOLDEN_PATH.write_text(
        json.dumps(
            {
                "_readme": [
                    "GENERATED by pytest --update-golden. Do not hand-edit.",
                    "",
                    "NOT PRODUCTION SCORES: percentile rank against the 20 fixture",
                    "laptops, not the ~238 in the catalog. ADR-0011's F16 figures",
                    "(57/59/62/61/56) come from the full catalog and will not match",
                    "anything here. See tests/golden/pickscore_fixture.json.",
                ],
                "snapshot": snapshot,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {GOLDEN_PATH}")
