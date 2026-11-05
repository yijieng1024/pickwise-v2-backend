"""The configurator's arithmetic and score (step 10c, step (d)), on the chip kits stored in
production on 2026-11-04 (tests/unit/fixtures/apple_kits_pro_2026-11-04.json, exported
from the post-backfill snapshot). Totals are checked against Apple's own update-config
totals for the same configurations. No database, no network."""
import json
import uuid
from pathlib import Path

import pytest

from ._adapters import apple_options, laptop_models, options_service

ROWS = json.loads((Path(__file__).parent / "fixtures" / "apple_kits_pro_2026-11-04.json").read_text(encoding="utf-8"))


def _kits(size, line):
    return apple_options.kits_from_rows([r for r in ROWS if r["size_inch"] == size and r["line"] == line])


@pytest.mark.parametrize("size, line, chip, memory, storage, apple_total", [
    (14, "liquid-retina", "m6-12-12", "32gb", "4tb", 16849.0),
    (14, "proled", "m5pro-18-20", "64gb", "2tb", 19699.0),
    (16, "proled", "m5max-18-40", "128gb", "8tb", 45499.0),
    (14, "liquid-retina", "m5pro-15-16", "48gb", "2tb", 15249.0),
    (16, "liquid-retina", "m5max-18-32", "36gb", "4tb", 22999.0),
])
def test_a_total_from_the_stored_kits_is_apples_own(size, line, chip, memory, storage, apple_total):
    q = apple_options.quote(_kits(size, line), chip, {"memory": memory, "storage": storage})
    assert (q.chip, q.total_rm, q.chip_changed) == (chip, apple_total, False)


def test_an_option_the_chip_does_not_allow_names_what_it_allows_and_which_chip_offers_it():
    kits = _kits(14, "liquid-retina")
    [problem] = apple_options.not_allowed(kits, "m5pro-15-16", {"memory": "16gb", "storage": "1tb"})
    assert problem == {"group": "memory", "value": "16gb", "allowed": ["24gb", "48gb"], "offered_by": ["m6-12-12"]}
    assert apple_options.not_allowed(kits, "m6-12-12", {"memory": "16gb", "storage": "1tb"}) == []


def _dist(*values):
    return {"min": min(values), "max": max(values), "values": sorted(values)}


SCORING = (
    {"price": _dist(4000.0, 8299.0, 20000.0), "ram_gb": _dist(16, 24, 64), "storage_gb": _dist(512, 1024, 4096),
     "weight_kg": _dist(1.2, 1.6, 2.5), "battery_wh": _dist(50.0, 72.0, 100.0),
     "cpu_mark": _dist(20000.0, 35823.0, 57244.0), "gpu_mark": _dist(5000.0, 9584.0, 20000.0)},
    [("ARM Apple M6 12 Core 0 MHz", 35823), ("Apple M5 Pro 18 Core", 57244)],
    [("GeForce RTX 4060 Laptop GPU", 17306), ("Intel Arc B390 GPU", 9000)],  # the 20- and 12-core Apple GPUs' equivalents
)
PRO14 = laptop_models.Laptop(
    id=uuid.uuid4(), brand_id=uuid.uuid4(), model_code="apple-macbook-pro-14-liquid-retina-m6-12c-12g-16gb-1024gb",
    product_name="MacBook Pro 14", price_rm=8299.0, processor_model="Apple M6 (12-core)", gpu_model="12-core GPU",
    ram_gb=16, ssd_gb=1024, display_size_inch=14.2, weight_kg=1.55, battery_wh=72.4)


def test_the_m6_scores_on_its_own_benchmarks():
    chip = {"cpu_model": "Apple M6 (12-core)", "gpu_model": "12-core GPU"}
    scores, reason = options_service.live_scores(PRO14, chip, {"memory": "32gb", "storage": "4tb"}, 16849.0, SCORING)
    assert reason is None
    assert all(isinstance(s["score"], int) for s in scores)
    assert not any(s["flags"]["gpu_benchmark_unresolved"] or s["flags"]["cpu_benchmark_unresolved"] for s in scores)


def test_a_chip_with_only_its_gpu_unresolved_scores_with_the_flagged_neutral_like_the_detail_page():
    chip = {"cpu_model": "Apple M5 Pro (18-core)", "gpu_model": "14-core GPU"}  # no Apple GPU equivalent
    scores, reason = options_service.live_scores(PRO14, chip, {"memory": "32gb", "storage": "4tb"}, 16849.0, SCORING)
    assert reason is None
    assert all(isinstance(s["score"], int) and s["flags"]["gpu_benchmark_unresolved"] for s in scores)
    assert not any(s["flags"]["cpu_benchmark_unresolved"] for s in scores)


def test_a_chip_with_neither_benchmark_gets_no_score_and_says_why():
    chip = {"cpu_model": "Apple M9 (14-core)", "gpu_model": "14-core GPU"}
    scores, reason = options_service.live_scores(PRO14, chip, {"memory": "32gb", "storage": "4tb"}, 16849.0, SCORING)
    assert scores is None
    assert "Apple M9 (14-core)" in reason and "14-core GPU" in reason


def test_a_chip_with_benchmarks_gets_the_detail_pages_five_scores():
    chip = {"cpu_model": "Apple M5 Pro (18-core)", "gpu_model": "20-core GPU"}
    scores, reason = options_service.live_scores(PRO14, chip, {"memory": "64gb", "storage": "2tb"}, 15000.0, SCORING)
    assert reason is None
    assert [s["use_case"] for s in scores] == ["office_study", "programming", "gaming", "creative_work", "general_use"]
    assert all(isinstance(s["score"], int) for s in scores)


class _Pref:
    """A preference profile as the engine reads it."""
    budget = {"min": None, "max": 9000.0}
    purpose = ["Programming/Development"]
    priorities = {"cpu": 10, "ram_storage": 9, "price": 3}
    screen_size = []
    portability = "Neutral"
    brand_preferences = ["apple"]


def test_the_personalized_score_is_the_engine_s_personalized_mode_on_the_configured_machine():
    chip = {"cpu_model": "Apple M6 (12-core)", "gpu_model": "12-core GPU"}
    choices = {"memory": "32gb", "storage": "4tb"}
    personal = options_service.personal_score(PRO14, chip, choices, 16849.0, SCORING, _Pref())
    product = options_service._configured_product(PRO14, chip, choices, 16849.0)
    expected = options_service.calculate_pick_score(product, _Pref(), *SCORING)
    assert personal["score"] == expected.score and personal["mode"] == "personalized"
    general, _ = options_service.live_scores(PRO14, chip, choices, 16849.0, SCORING)
    assert personal["score"] != next(s["score"] for s in general if s["use_case"] == "general_use")


def test_no_profile_means_no_personalized_score(monkeypatch):
    kits = _kits(14, "liquid-retina")
    c = options_service.Configurable(PRO14, [r for r in ROWS if r["size_inch"] == 14 and r["line"] == "liquid-retina"],
                                     kits, "m6-12-12", {"memory": "16gb", "storage": "1tb"})
    monkeypatch.setattr(options_service, "get_scoring_data", lambda session: SCORING)
    choices = options_service.choices_for(c, "m6-12-12", {})
    assert options_service.configure(None, c, "m6-12-12", choices)["personalized_score"] is None
    assert options_service.configure(None, c, "m6-12-12", choices, user_pref=_Pref())["personalized_score"]["score"]
