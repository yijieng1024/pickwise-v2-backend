"""The M6's benchmarks (ADR-0010 Amendment III), against production's PassMark rows of
2026-11-04: every CPU row with "apple" in its name (a stripped "ARM Apple" key starts with
"apple", so only such a row could collide with it) and the GPU rows around the Apple ladder."""
import json
from collections import Counter
from pathlib import Path

from ._adapters import apple_gpu_map, normalize, resolve_cpu, resolve_gpu

TABLES = json.loads((Path(__file__).parent / "fixtures" / "passmark_apple_2026-11-04.json").read_text(encoding="utf-8"))
CPU = [tuple(r) for r in TABLES["cpu"]]
GPU = [tuple(r) for r in TABLES["gpu"]]
ARM_APPLE = [name for name, _ in CPU if name.startswith("ARM Apple")]


def test_the_m6_gpu_resolves_to_the_arc_b390():
    assert apple_gpu_map()["12 core gpu"] == "Intel Arc B390 GPU"
    result = resolve_gpu("12-core GPU", "Apple M6 (12-core)", GPU)
    assert (result["matched_name"], result["resolution"]) == ("Intel Arc B390 GPU", "apple_equivalent")


def test_the_m6_cpu_resolves_exactly():
    result = resolve_cpu("Apple M6 (12-core)", CPU)
    assert (result["matched_name"], result["match_confidence"]) == ("ARM Apple M6 12 Core 0 MHz", 1.0)


def test_the_gpu_ladder_keeps_its_order():
    marks = [resolve_gpu(gpu, cpu, GPU)["score"] for gpu, cpu in (
        ("10-core GPU", "Apple M5 (10-core)"), ("12-core GPU", "Apple M6 (12-core)"), ("16-core GPU", "Apple M5 Pro (15-core)"))]
    assert marks == sorted(marks) and len(set(marks)) == 3, marks


def test_the_arm_apple_rows_lose_only_the_prefix_and_clock_and_collide_with_nothing():
    assert len(ARM_APPLE) == 5
    assert normalize("ARM Apple M5 Ultra 30 Core 0 MHz") == "apple m5 ultra 30 core"
    keys = Counter(normalize(name) for name, _ in CPU)
    assert all(keys[normalize(name)] == 1 for name in ARM_APPLE)
    assert normalize("ARM Cortex-A78 8 Core 0 MHz") == "arm cortex-a78 8 core 0 mhz", "only Apple rows change"
