"""The Nuxt 3 (__NUXT_DATA__ / devalue) reviver is exact or it raises."""
import json
import math

import pytest

from ._adapters import nuxt_state


def _page(flat: list) -> str:
    return f'<html><script type="application/json" id="__NUXT_DATA__">{json.dumps(flat)}</script></html>'


def test_special_indices_shared_references_and_wrappers():
    flat = [
        ["ShallowReactive", 1],
        {"missing": -1, "first": 2, "again": 2, "wrapped": 3, "nan": -3},
        {"name": 4},
        ["Reactive", 5],
        "shared",
        "inside the wrapper",
        "LAST",  # what -1 used to come back as, read as a Python index
    ]
    state = nuxt_state.nuxt3_state(_page(flat))
    assert state["missing"] is None
    assert state["first"] is state["again"] == {"name": "shared"}
    assert state["wrapped"] == "inside the wrapper"
    assert math.isnan(state["nan"])


def test_cycle_raises():
    with pytest.raises(nuxt_state.NuxtStateError, match="cycle"):
        nuxt_state.nuxt3_state(_page([{"self": 0}]))


def test_unknown_type_tag_raises():
    with pytest.raises(nuxt_state.NuxtStateError, match="unknown devalue type tag 'Mystery'"):
        nuxt_state.nuxt3_state(_page([["Mystery", 1], "x"]))


def test_page_without_payload_raises():
    with pytest.raises(nuxt_state.NuxtStateError, match="no __NUXT_DATA__"):
        nuxt_state.nuxt3_state("<html><body>Just a moment...</body></html>")
