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


# --------------------------------------------------------------------------
# Nuxt 2: window.__NUXT__ read as literals, never executed
# --------------------------------------------------------------------------


BS = chr(92)  # one literal backslash: these sources are what a page holds


def _nuxt2(params: str, body: str, args: str) -> str:
    return f"<script>window.__NUXT__=(function({params}){{{body}}}({args}));</script>"


def test_nuxt2_binds_arguments_and_reads_shorthand_keys_and_void():
    state = nuxt_state.nuxt2_state(_nuxt2(
        "a,b,c", 'return {state:{a,x:b,y:c,z:void 0,big:Array(2),"q r":1,5:true,n:null}}', '"one",2.5',
    ))
    assert state["state"] == {
        "a": "one", "x": 2.5, "y": None, "z": None, "big": [None, None], "q r": 1, "5": True, "n": None,
    }


def test_nuxt2_applies_the_statements_before_return():
    # Every real ROG page has these, and shared data hangs off them
    state = nuxt_state.nuxt2_state(_nuxt2(
        "a,b,c", 'a.name=c;a.more="x;y";b[1]={from:a};return {state:{a,b}}', '{},Array(2),"G835L"',
    ))
    assert state["state"]["a"] == {"name": "G835L", "more": "x;y"}
    assert state["state"]["b"] == [None, {"from": {"name": "G835L", "more": "x;y"}}]


@pytest.mark.parametrize("body", [
    "a.b.c=1;return {}",      # chained
    "doSomething();return {}",  # a call
    "b[0]=1;return {}",       # an index past the array's size
    "c.x=1;return {}",        # a name nothing bound
    "if(a){};return {}",
])
def test_nuxt2_other_statements_before_return_raise(body):
    with pytest.raises(nuxt_state.NuxtStateError):
        nuxt_state.nuxt2_state(_nuxt2("a,b", body, "{},Array(0)"))


def test_nuxt2_unknown_identifier_raises():
    with pytest.raises(nuxt_state.NuxtStateError, match="unknown identifier 'mystery'"):
        nuxt_state.nuxt2_state(_nuxt2("a", "return {x:mystery}", "1"))


def test_nuxt2_unknown_shorthand_raises():
    with pytest.raises(nuxt_state.NuxtStateError, match="unknown identifier 'mystery'"):
        nuxt_state.nuxt2_state(_nuxt2("a", "return {mystery}", "1"))


def test_nuxt2_hex_escape_decodes():
    source = f'return {{x:"{BS}x41{BS}u002F{BS}n"}}'
    state = nuxt_state.nuxt2_state(_nuxt2("a", source, "1"))
    assert state["x"] == "A/" + chr(10)


def test_nuxt2_surrogate_pair_decodes_to_one_character():
    source = f'return {{x:"{BS}uD83D{BS}uDE00"}}'
    state = nuxt_state.nuxt2_state(_nuxt2("a", source, "1"))
    assert state["x"] == "\U0001F600" and len(state["x"]) == 1


def test_nuxt2_nan_infinity_and_hex_numbers():
    state = nuxt_state.nuxt2_state(_nuxt2("a", "return {n:NaN,i:Infinity,m:-Infinity,h:0xFF,e:1e3,d:.5,z:-1}", "1"))
    assert math.isnan(state["n"])
    assert (state["i"], state["m"], state["h"], state["e"], state["d"], state["z"]) == (
        math.inf, -math.inf, 255, 1000.0, 0.5, -1,
    )


@pytest.mark.parametrize("source", [
    "(function(a){return {x:1}}(1)",   # the wrapper never closes
    "(function(a){return {x:1}(1));",  # the body never closes
    "(function(a){return {x:`t`}}(1));",  # a template literal
    "(function(a){return {x:1}}(1));tail()",  # code after the value
    '(function(a){return {x:"never closed}}(1));',
])
def test_nuxt2_malformed_source_raises_rather_than_parsing_garbage(source):
    with pytest.raises(nuxt_state.NuxtStateError):
        nuxt_state.nuxt2_state(f"<script>window.__NUXT__={source}</script>")


def test_nuxt2_page_without_state_raises():
    with pytest.raises(nuxt_state.NuxtStateError, match=r"no window\.__NUXT__"):
        nuxt_state.nuxt2_state("<html><body>Just a moment...</body></html>")
