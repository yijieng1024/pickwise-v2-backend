"""
Shared pytest fixtures for PickWise.

Put this file at <repo-root>/tests/conftest.py. pytest auto-discovers it and
makes every fixture below available to any test in tests/ without importing.

NOTE ON SCOPE: everything in tests/unit/ must run with NO database, NO network
and NO API key. If a test needs any of those, it belongs in tests/integration/.
That separation is what lets the unit suite stay in the pull-request gate.
"""

import sys
from pathlib import Path

import pytest

# Let `import app.…` work when pytest is run from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help=(
            "Rewrite tests/golden/pickscore.json from the current engine output. "
            "Deliberate and explicit on purpose: regenerating must never happen "
            "automatically on failure, or the snapshot records whatever the last "
            "change did rather than what anyone agreed to."
        ),
    )


@pytest.fixture
def ranges():
    """
    The real active-only catalog ranges measured 2026-08-17.

    Hardcoded on purpose. If these came from the live database, the tests would
    change meaning every time a laptop is added — and a test whose expected
    value drifts on its own is not a test, it is a log.
    """
    return {
        "price_rm": (1429.0, 36999.0),
        "ram_gb": (4.0, 128.0),
        "storage_gb": (64.0, 4096.0),
        "weight_kg": (0.79, 3.73),
        "battery_wh": (36.5, 100.0),
        "cpu_mark": (767.0, 62748.0),
        "gpu_mark": (1299.0, 28248.0),
    }


@pytest.fixture
def f16():
    """
    The TUF Gaming F16 reference machine, used as the control throughout the
    normalization work. Same machine, same numbers, so any factor test written
    against it can be compared straight back to the figures in ADR-0011.
    """
    return {
        "laptop_id": "asus-tuf-gaming-f16-i7-14650hx-rtx5060-16gb-1tb",
        "model_code": "FX608JMR",
        "price_rm": 5899.0,
        "processor_model": "Intel Core i7-14650HX",
        "gpu_model": "NVIDIA GeForce RTX 5060 Laptop GPU",
        "ram_gb": 16,
        "storage_gb": 1024,
        "storage_type": "SSD",
        "weight_kg": 2.2,
        "display_size_inch": 16.0,
        "battery_wh": 90.0,
    }


@pytest.fixture
def hdd_laptop(f16):
    """Same machine but on a spinning disk, to isolate the -15 storage penalty."""
    return {**f16, "storage_type": "HDD"}
