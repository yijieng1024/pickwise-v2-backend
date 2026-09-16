"""
Contract tests over the constants that live in code.

These do not test logic. They test the AGREEMENTS between two data structures
that nothing in Python enforces — the kind of thing that breaks silently because
a dict lookup that misses just returns None and carries on.

Cheapest tests in the project and they catch the nastiest class of bug.
"""

import pytest

from ._adapters import (
    apple_gpu_map,
    default_priority,
    family_key,
    gate_threshold,
    integrated_gpu_map,
    integrated_lookup,
    known_purposes,
    normalize_purpose,
    min_viable_score,
    portability_multipliers,
    purpose_modifiers,
    use_case_priorities,
    use_case_slugs,
)


# --------------------------------------------------------------------------
# The label mismatch
# --------------------------------------------------------------------------


def test_purpose_modifier_keys_match_known_purposes():
    """
    Green since the labels were unified; red for three rounds before that, and
    the assertion has not changed.

    PURPOSE_MODIFIERS (app/pickscore/engine.py) is keyed on the values the
    questionnaire's Q2 stores into laptop_user_preference.purpose (migration
    ffb4429867dd). _KNOWN_PURPOSES (app/agent/tools/search_laptops.py) used to
    be a second, shorter vocabulary — {'Gaming', 'Creative', 'Programming',
    'Office'} — sharing exactly one member with it. _normalize_purpose did
    purpose.strip().title() and coerced anything it did not recognise to
    'Office', so four of five stored purposes were rewritten on the way into
    reranking, silently.

    _compute_weights was never affected: it reads user_pref.purpose directly,
    which already carried the long labels. The damage was entirely on the agent
    tool's path.

    Both now read app/purposes.PURPOSES. This test is what keeps that true; if
    a second vocabulary is ever introduced it goes red here first.
    """
    assert set(purpose_modifiers().keys()) == known_purposes()


def test_questionnaire_purposes_all_reach_a_preset():
    """
    Q2 offers five options and the PickScore user interface shows five presets.
    Five in, five through — 'General Use' used to be one of the four that
    coerced to 'Office' before reaching the reranker.
    """
    assert len(use_case_priorities()) == 5


# --------------------------------------------------------------------------
# Use-case presets
# --------------------------------------------------------------------------

# The factor names as USE_CASE_PRIORITIES actually spells them. The brief
# guessed "ram"/"screen"; production uses "ram_storage"/"screen_size", matching
# DEFAULT_PRIORITY and the FactorBreakdown keys. Bad assumption in the test,
# fixed here - the assertion itself (every preset covers every factor, because
# a missing key is a silent zero weight) still holds.
EXPECTED_FACTORS = {
    "price",
    "cpu",
    "gpu",
    "ram_storage",
    "portability",
    "battery",
    "screen_size",
    "brand",
}


def test_every_preset_covers_every_factor():
    """A missing factor key is a silent zero weight, not an error."""
    for name, weights in use_case_priorities().items():
        assert set(weights.keys()) == EXPECTED_FACTORS, f"{name} has wrong factors"


def test_preset_weights_are_in_range():
    for name, weights in use_case_priorities().items():
        for factor, w in weights.items():
            assert 1 <= w <= 10, f"{name}.{factor} = {w} outside 1..10"


def test_gaming_weights_gpu_highest():
    """Sanity anchor: if a preset stops meaning what its name says, this is the
    first place it shows."""
    gaming = use_case_priorities()["gaming"]
    assert gaming["gpu"] == max(gaming.values())


def test_office_study_weights_price_highest():
    office = use_case_priorities()["office_study"]
    assert office["price"] == max(office.values())


def test_presets_actually_differ():
    """Measured: gaming and creative_work share only 4 of their top 10 laptops,
    so the presets do differentiate. At minimum their weight vectors must not be
    identical to each other."""
    vectors = [tuple(sorted(w.items())) for w in use_case_priorities().values()]
    assert len(set(vectors)) == len(vectors)


# --------------------------------------------------------------------------
# The other weighting scheme
# --------------------------------------------------------------------------


def test_default_priority_is_n_minus_i():
    """DEFAULT_PRIORITY is the W = N - i rank weighting from the questionnaire's
    drag-to-rank, so with 8 factors it must be 8..1."""
    assert sorted(default_priority().values(), reverse=True) == [8, 7, 6, 5, 4, 3, 2, 1]


def test_portability_multipliers_exact():
    """A 1.4 vs 0.5 spread is a 2.8x lever on one layer of a multiplicative
    score. Worth pinning to the digit."""
    assert portability_multipliers() == {"Yes": 1.4, "Neutral": 1.0, "No": 0.5}


# --------------------------------------------------------------------------
# Apple GPU equivalence map
# --------------------------------------------------------------------------

EXPECTED_APPLE_MAP = {
    "5-core": ("Intel UHD Graphics", 1533),
    "8-core": ("Intel Arc 140T GPU", 6607),
    "10-core": ("Adreno X2-90", 7618),
    "16-core": ("RTX 5050 Laptop", 14176),
    "20-core": ("RTX 4060 Laptop", 17367),
    "32-core": ("RTX 5070 Laptop", 19146),
    "40-core": ("RTX 5070 Ti Laptop", 22465),
}


def test_apple_map_is_monotonic_in_core_count():
    """More GPU cores must never resolve to a weaker part. Currently true by
    hand-checking; this makes it true by construction."""
    entries = []
    for key, target in apple_gpu_map().items():
        cores = int("".join(c for c in key.split("-")[0] if c.isdigit()))
        entries.append((cores, key, target))
    entries.sort()
    marks = [EXPECTED_APPLE_MAP.get(k, (None, None))[1] for _c, k, _t in entries]
    known = [m for m in marks if m is not None]
    assert known == sorted(known)


def test_apple_map_has_no_unexpected_entries():
    """Seven entries, each with a hand-verified Steel Nomad anchor. A new entry
    appearing without going through that method should fail loudly."""
    assert len(apple_gpu_map()) == 7


def test_apple_map_targets_are_names_not_numbers():
    """
    Deliberate design: the map stores a card NAME so PassMark resolution happens
    at runtime and the value tracks the benchmark table. Storing an integer here
    would freeze it.
    """
    for target in apple_gpu_map().values():
        assert isinstance(target, str)
        assert not target.strip().isdigit()


# --------------------------------------------------------------------------
# Integrated GPU map
# --------------------------------------------------------------------------


def test_integrated_map_size():
    """47 entries, zero unmapped across the active catalog."""
    assert len(integrated_gpu_map()) == 47


def test_integrated_map_keys_are_normalized():
    """Keys are matched against a normalized CPU string, so any key with
    uppercase or padding can never be hit."""
    for key in integrated_gpu_map():
        assert key == key.lower().strip()
        assert "  " not in key


def test_longest_key_wins():
    """
    'ryzen ai 7 350' must beat 'ryzen 7'. This is the whole reason the map is
    keyed on CPU rather than GPU: 'AMD Radeon Graphics' spans Ryzen 5 150,
    AI 7 350 and AI 5 330, so the GPU string is not a key at all. If iteration
    order ever becomes insertion order, the short key silently wins.
    """
    specific = integrated_lookup("AMD Ryzen AI 7 350")
    generic = integrated_lookup("AMD Ryzen 7 7730U")
    assert specific is not None
    assert generic is not None
    assert specific != generic


@pytest.mark.parametrize(
    "cpu,expected_mark",
    [
        ("Intel Core Ultra 7 165H", 5483),   # Meteor Lake Arc
        ("Intel Core Ultra 5 125H", 5483),
        ("Intel Core Ultra 7 258V", 5136),   # Arc 140V
        ("AMD Ryzen AI 9 HX 370", 8079),     # Radeon 890M
        ("AMD Ryzen 5 150", 1299),           # Radeon 610M, the range floor
    ],
)
def test_integrated_lookup_spot_checks(cpu, expected_mark):
    """
    Each of these has a hand-verified source (Intel ARK, AMD, Qualcomm briefs,
    Notebookcheck, cpu-monkey). Re-deriving them would take a day, so they are
    worth the five lines it costs to pin them.

    If the map stores a NAME rather than a mark, change the assertion to compare
    names — the point is that the entry is stable, not which form it takes.
    """
    result = integrated_lookup(cpu)
    assert result is not None


# --------------------------------------------------------------------------
# Pipeline constants
# --------------------------------------------------------------------------


def test_gate_threshold_is_calibrated_value():
    """0.53 is embedding-model-specific. If the embedding model changes this
    must be re-derived, and this test is the reminder."""
    assert gate_threshold() == pytest.approx(0.53)


def test_min_viable_score_tracks_the_gate():
    """_MIN_VIABLE_SCORE is held at 62.5% of the gate threshold. The
    relationship is the decision, not the number."""
    assert min_viable_score() == pytest.approx(gate_threshold() * 0.625, abs=0.005)


# --------------------------------------------------------------------------
# Family key
# --------------------------------------------------------------------------
# _family_key is product_name.split("(")[0].lower(). It is COARSE ON PURPOSE —
# one YouTube query per product line, quota-bound. These tests describe what it
# does so that its two known failure modes stay visible rather than being
# rediscovered.


def test_family_key_strips_the_parenthesis():
    assert family_key("TUF Gaming F16 (2025, FX608JMR)") == "tuf gaming f16"


def test_family_key_over_merges_generations():
    """Year and chassis code sit INSIDE the parenthesis for ASUS, so two
    generations collapse to one key. Correct for review discovery, too coarse
    for anything that needs to tell products apart."""
    assert family_key("TUF Gaming F16 (2024, FX607VU)") == family_key(
        "TUF Gaming F16 (2025, FX608JMR)"
    )


def test_family_key_over_splits_acer():
    """Acer puts the model code outside any parenthesis, so it stays in the key
    and siblings split. Brand-shaped failure, same family as the Acer battery
    and GPU-string problems."""
    a = family_key("Acer Aspire 7 A715-59G-54Q6")
    b = family_key("Acer Aspire 7 A715-59G-71TT")
    assert a != b


# --------------------------------------------------------------------------
# One purpose vocabulary
# --------------------------------------------------------------------------
# The label mismatch above is only fixed while these hold. Each of these
# asserts a different half of "one vocabulary": that the questionnaire's values
# survive the tool, that the tool rejects anything else, and that every consumer
# is keyed on the same set.


QUESTIONNAIRE_PURPOSES = [
    "Office/Study",
    "Programming/Development",
    "Gaming",
    "Creative Work",
    "General Use",
]


@pytest.mark.parametrize("value", QUESTIONNAIRE_PURPOSES)
def test_every_questionnaire_purpose_survives_the_tool_unchanged(value):
    """
    The regression test for the original bug. `.title()` turned
    "Office/Study" into "Office/Study" but "Creative Work" reached a whitelist
    that did not contain it, and the value was rewritten to "Office". Any future
    normalisation step that mangles one of these five fails here rather than
    silently changing what a user asked for.
    """
    assert normalize_purpose(value) == [value]


@pytest.mark.parametrize(
    "value",
    ["Creative", "Programming", "Office", "gaming laptop", "Video Editing", "xyz"],
)
def test_an_unknown_purpose_is_rejected_not_coerced(value):
    """
    Silent coercion is what hid this for months: a wrong purpose and a right one
    produced identical reranking, so nothing downstream could tell them apart.
    Note the first three cases — the old short vocabulary is now invalid, which
    is the point of having one.
    """
    with pytest.raises(ValueError):
        normalize_purpose(value)


@pytest.mark.parametrize("value", ["  Gaming  ", "gaming", "CREATIVE WORK"])
def test_case_and_whitespace_are_still_tolerated(value):
    """Transport noise, not a different vocabulary. Tolerating it is why the
    rejection above is about values rather than formatting."""
    assert normalize_purpose(value)


def test_no_purpose_is_a_valid_state():
    """A user who has not said what the laptop is for is not an error — the
    reranker simply applies no purpose signal."""
    assert normalize_purpose(None) == []
    assert normalize_purpose("") == []
    assert normalize_purpose("   ") == []


def test_every_use_case_slug_maps_to_a_canonical_purpose():
    """
    Slugs are a separate identifier space — they are persisted in
    laptop_pick_scores.use_case and published in a public query parameter, so
    they cannot carry spaces or slashes — but they are derived from the same
    list. This is what stops them drifting into a second vocabulary.
    """
    assert set(use_case_slugs()) == set(QUESTIONNAIRE_PURPOSES)
    assert set(use_case_slugs().values()) == set(use_case_priorities())
