"""
The go-live check (plan step 11, decision 4): one pure function that returns the
conditions a laptop fails. Each condition is failed here on its own, so a condition
that stopped being checked fails exactly one case.
"""
from types import SimpleNamespace

import pytest

from tests.unit._adapters import go_live_failed_conditions as failed_conditions

CPU = [("Intel Core i7-14650HX", 33467)]
GPU = [("GeForce RTX 5060 Laptop GPU", 15000)]


def _laptop(**over):
    fields = dict(price_rm=4000.0, processor_model="Intel Core i7-14650HX",
                  gpu_model="NVIDIA GeForce RTX 5060 Laptop GPU", ram_gb=16, ssd_gb=512,
                  display_size_inch=16.0, weight_kg=2.2, family_id=None)
    return SimpleNamespace(**{**fields, **over})


def test_a_complete_laptop_passes_and_a_null_family_does_not_block():
    assert failed_conditions(_laptop(family_id=None), 0, CPU, GPU) == []


@pytest.mark.parametrize("over, failed", [
    ({"price_rm": 0.0}, ["price"]),
    ({"ram_gb": 0}, ["ram"]),
    ({"ram_gb": None}, ["ram"]),
    ({"ssd_gb": 0}, ["storage"]),
    ({"display_size_inch": None}, ["screen_size"]),
    ({"weight_kg": None}, ["weight"]),
    # A missing part has no benchmark either, so the two are reported together.
    ({"processor_model": " "}, ["cpu", "cpu_benchmark"]),
    ({"gpu_model": ""}, ["gpu", "gpu_benchmark"]),
    # Present, but nothing in the benchmark list is that part.
    ({"processor_model": "Zhaoxin KX-9999 Imaginary"}, ["cpu_benchmark"]),
    ({"gpu_model": "Imaginary Graphics 9999X"}, ["gpu_benchmark"]),
])
def test_each_condition_fails_on_its_own(over, failed):
    assert failed_conditions(_laptop(**over), 0, CPU, GPU) == failed


def test_another_laptop_with_the_same_model_code_fails_it():
    assert failed_conditions(_laptop(), 1, CPU, GPU) == ["duplicate_model_code"]


def test_every_failed_condition_is_returned_not_only_the_first():
    assert failed_conditions(_laptop(price_rm=0.0, weight_kg=None), 2, [], []) == [
        "price", "weight", "duplicate_model_code", "cpu_benchmark", "gpu_benchmark",
    ]
