"""
Why a factor scored what it did, in the breakdown's `note`.

The GPU notes were dead code until 2026-10-25: _score_gpu looked them up by
resolve_gpu_benchmark's `resolution`, a key it never returned, so every proxy
or unresolved GPU went out with note=None and the frontend guessed ("Apple
silicon..." on integrated Intel graphics). Notes change no number -- the
golden snapshot proves that side.
"""
from ._adapters import pick_score, resolve_gpu, score_gpu_note

_TABLE = [
    ("GeForce RTX 5060 Laptop GPU", 16785),
    ("Intel Arc 140T GPU", 6607),
]


def test_every_gpu_path_reports_its_resolution():
    cases = {
        "direct": ("GeForce RTX 5060 Laptop GPU", "Intel Core i7-14650HX"),
        "laptop_variant": ("NVIDIA GeForce RTX 5060", "Intel Core i7-14650HX"),
        "apple_equivalent": ("8-core GPU", "Apple M4"),  # the catalogue's own string
        "integrated": ("Intel Arc Graphics", "Intel Core Ultra 7 255H"),
        "unresolved": ("Unknown", "Unknown"),
    }
    for expected, (gpu, cpu) in cases.items():
        assert resolve_gpu(gpu, cpu, _TABLE)["resolution"] == expected, gpu


def test_an_anchored_gpu_with_no_mark_is_unresolved():
    """A model number that resolves to no mark is not "direct"."""
    assert resolve_gpu("GeForce RTX 5060 Laptop GPU", "Unknown", [])["resolution"] == "unresolved"


def test_proxy_and_unresolved_gpus_carry_their_reason(ranges):
    assert score_gpu_note("GeForce RTX 5060 Laptop GPU", ranges, benchmarks=_TABLE) is None
    assert "Apple" in score_gpu_note("8-core GPU", ranges, "Apple M4", _TABLE)
    assert "Integrated" in score_gpu_note("Intel Arc Graphics", ranges, "Intel Core Ultra 7 255H", _TABLE)
    assert "could not be identified" in score_gpu_note("Unknown", ranges, "Unknown", _TABLE)


def test_general_mode_says_brand_and_screen_size_are_not_scored(ranges):
    result = pick_score("Unknown", "Unknown", ranges)
    notes = {b.factor: b.note for b in result.breakdown}
    for factor in ("brand", "screen_size"):
        assert "Not scored" in notes[factor] and "50" in notes[factor]
