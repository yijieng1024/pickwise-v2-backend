"""The laptop listing states (ADR-0009, Amendment II 2026-11-04)."""
import pytest

from ._adapters import laptop_models


def test_discontinued_is_a_valid_status():
    assert laptop_models.LaptopUpdate(status="discontinued").status == "discontinued"


def test_an_unknown_status_is_still_refused():
    with pytest.raises(ValueError):
        laptop_models.LaptopUpdate(status="retired")
