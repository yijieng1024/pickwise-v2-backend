"""Review C: where a window's answer is stored (processor.place) and which
answers are accepted (processor.parse_about). No database."""
import uuid

import pytest

from tests.unit._adapters import review_processor as p

F, G, H = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
F_C1, F_C2, G_LINK, H_ONLY = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _cands():
    # F: separable family, configs listed, family-only link.
    # G: configs not listed, the link names one config.
    # H: configs not listed, one active configuration.
    return p.Candidates(
        ids={"F1": (F, None), "F1.C1": (F, F_C1), "F1.C2": (F, F_C2), "F2": (G, None), "F3": (H, None)},
        link_config={F: None, G: G_LINK, H: None},
        sole_member={F: None, G: None, H: H_ONLY},
    )


def test_a_named_configuration_is_attached_to_it_in_either_scope():
    c = _cands()
    assert p.place("F1.C2", "config", c) == {"family_id": F, "laptop_id": F_C2, "scope": "config"}
    # A family-wide claim from a known unit: family chunk, tested on that config.
    assert p.place("F1.C1", "family", c) == {"family_id": F, "laptop_id": F_C1, "scope": "family"}


def test_a_family_claim_names_the_linked_config_when_there_is_one():
    c = _cands()
    assert p.place("F1", "family", c) == {"family_id": F, "laptop_id": None, "scope": "family"}
    assert p.place("F2", "family", c) == {"family_id": G, "laptop_id": G_LINK, "scope": "family"}


def test_a_config_claim_with_no_known_config_is_not_attached():
    c = _cands()
    assert p.place("F1", "config", c) == {"reason": "config_unknown", "family_id": F}
    # Known through the link, or because the family has one configuration.
    assert p.place("F2", "config", c) == {"family_id": G, "laptop_id": G_LINK, "scope": "config"}
    assert p.place("F3", "config", c) == {"family_id": H, "laptop_id": H_ONLY, "scope": "config"}


@pytest.mark.parametrize("about,reason", [
    ("OTHER", "other_laptop"), ("SEVERAL", "several"), ("NONE", "none"),
])
def test_fixed_answers_are_not_attached(about, reason):
    assert p.place(about, "family", _cands()) == {"reason": reason, "family_id": None}


def test_only_offered_ids_are_accepted():
    c = _cands()
    assert p.parse_about(" f1.c2 ", c) == "F1.C2"
    assert p.parse_about("other", c) == "OTHER"
    for bad in ("F4", "F1.C3", "MacBook Air", ""):
        with pytest.raises(p.InvalidAnswer):
            p.parse_about(bad, c)


def test_each_window_carries_the_tail_of_the_previous_one():
    segs = [{"start": float(i * 15), "duration": 15.0, "text": f"part {i} " + "x" * 400} for i in range(6)]
    ws = p.windows_with_context(segs)
    assert ws[0]["previous"] == ""
    assert ws[1]["previous"] == ws[0]["text"][-p._CONTEXT_CHARS:]
    assert len(ws[1]["previous"]) == p._CONTEXT_CHARS
