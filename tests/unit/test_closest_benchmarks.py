"""closest_benchmarks / gpu_diagnosis (app/pickscore/benchmark_service.py), the
admin Pick Score page's diagnosis. They must never become a second matching
rule: whenever the matcher resolves a string, the first row shown is the row
it chose."""
import pytest

from tests.unit._adapters import (
    closest_benchmarks,
    cpu_confidence_threshold,
    gpu_confidence_threshold,
    gpu_diagnosis,
    resolve_cpu,
    resolve_gpu,
)

CPU_CONFIDENCE_THRESHOLD = cpu_confidence_threshold()
GPU_CONFIDENCE_THRESHOLD = gpu_confidence_threshold()

CPUS = [
    ("Intel Core i7-14650HX", 33000),
    ("Intel Core i7-14700HX", 36000),
    ("Intel Core Ultra 7 255H", 30000),
    ("AMD Ryzen 7 8845HS", 28000),
    ("AMD Athlon 64 X2 4200+", 767),
    ("Qualcomm Snapdragon X Elite - X1E-78-100", 22695),
    ("Qualcomm Snapdragon X Elite - X1E-78-100 - Qualcomm Or", 21000),
    ("Qualcomm Snapdragon X Plus - X1P42100", 17905),
    ("Apple M5 10 Core", 26000),
]
GPUS = [
    ("GeForce RTX 5060 Laptop GPU", 18000),
    ("GeForce RTX 5060", 22000),
    ("GeForce RTX 5070 Ti Laptop GPU", 24000),
    ("GeForce RTX 4060 Laptop GPU", 16000),
    ("Intel Arc 140T GPU", 4500),
    ("Radeon 780M", 6000),
]


@pytest.mark.parametrize("cpu", [
    "Intel Core i7-14650HX",
    "Intel® Core™ i7-14700HX Processor",
    "Intel Core Ultra 7 255H",
    "AMD Ryzen 7 8845HS",
    "Snapdragon X Elite X1E 78 100",
    "Snapdragon X Plus X1P-42-100",
    "Apple M5 (10-core)",
])
def test_first_cpu_row_is_the_matchers_choice(cpu):
    resolved = resolve_cpu(cpu, CPUS)
    assert resolved["score"] is not None
    assert closest_benchmarks(cpu, CPUS, CPU_CONFIDENCE_THRESHOLD)["candidates"][0]["name"] == resolved["matched_name"]


@pytest.mark.parametrize("gpu, cpu", [
    ("NVIDIA GeForce RTX 5060 Laptop GPU", "Intel Core i7-14650HX"),
    ("NVIDIA GeForce RTX 5060", "Intel Core i7-14650HX"),  # laptop-variant rewrite
    ("40-core GPU", "Apple M5 Max"),                  # Apple equivalence
    ("Intel Arc Graphics", "Intel Core Ultra 7 255H"),      # integrated-GPU map
])
def test_first_gpu_row_is_the_matchers_choice(gpu, cpu):
    resolved = resolve_gpu(gpu, cpu, GPUS)
    assert resolved["score"] is not None
    assert gpu_diagnosis(gpu, cpu, GPUS)["candidates"][0]["name"] == resolved["matched_name"]


def test_unresolved_cpu_shows_rows_below_the_threshold():
    cpu = "Intel Core i3-N305"
    assert resolve_cpu(cpu, CPUS)["score"] is None
    d = closest_benchmarks(cpu, CPUS, CPU_CONFIDENCE_THRESHOLD)
    assert d["kind"] == "fuzzy" and len(d["candidates"]) == 3
    assert all(c["confidence"] < CPU_CONFIDENCE_THRESHOLD for c in d["candidates"])
    assert d["threshold"] == CPU_CONFIDENCE_THRESHOLD


def test_missing_qualcomm_part_says_so():
    d = closest_benchmarks("Snapdragon® X2 Plus X2P-64-100", CPUS, CPU_CONFIDENCE_THRESHOLD)
    assert d["kind"] == "qualcomm_part_missing"
    assert d["message"] == "Exact part number X2P64100: no row carries it"
    assert d["candidates"]


def test_placeholder_has_no_candidates():
    d = closest_benchmarks("Unknown", CPUS, CPU_CONFIDENCE_THRESHOLD)
    assert d["kind"] == "placeholder" and d["candidates"] == []


def test_gpu_with_no_model_number_names_the_missing_map_entry():
    d = gpu_diagnosis("Intel Graphics", "Intel Core i3-N305", GPUS)
    assert d["kind"] == "no_igpu_entry" and d["candidates"] == []
    assert d["message"] == "No integrated-GPU entry for CPU Intel Core i3-N305"
    assert d["threshold"] == GPU_CONFIDENCE_THRESHOLD
