"""Which configuration a review's chunks go to, from its links alone (Review A).

Human links replace auto links; a review is processable only when its effective
links name exactly one configuration. Pure functions, no database.
"""
import uuid
from types import SimpleNamespace

from ._adapters import review_link_service as ls

A, B = uuid.uuid4(), uuid.uuid4()


def _l(source, laptop_id):
    return SimpleNamespace(match_source=source, laptop_id=laptop_id)


def test_one_auto_config_is_processable():
    assert ls.processing_laptop_id([_l("auto", A)]) == A


def test_no_links_or_family_only_is_not():
    assert ls.processing_laptop_id([]) is None
    assert ls.processing_laptop_id([_l("human", None)]) is None


def test_a_human_family_only_link_overrules_an_auto_config():
    assert ls.processing_laptop_id([_l("auto", A), _l("human", None)]) is None


def test_a_human_config_beats_a_different_auto_config():
    assert ls.processing_laptop_id([_l("auto", A), _l("human", B)]) == B


def test_several_configs_are_not_processable():
    assert ls.processing_laptop_id([_l("human", A), _l("human", B)]) is None
    assert ls.processing_laptop_id([_l("human", A), _l("human", A)]) == A


# --- processing_target (Review B): one family, at most one config ------------

F, G = uuid.uuid4(), uuid.uuid4()


def _fl(source, family_id, laptop_id=None):
    return SimpleNamespace(match_source=source, family_id=family_id, laptop_id=laptop_id)


def test_target_one_config_is_config_scope():
    assert ls.processing_target([_fl("auto", F, A)]) == (F, A)


def test_target_family_only_is_family_scope():
    assert ls.processing_target([_fl("human", F)]) == (F, None)
    # A human family-only link overrules an auto config: family scope.
    assert ls.processing_target([_fl("auto", F, A), _fl("human", F)]) == (F, None)


def test_target_config_plus_its_own_family_link_is_the_config():
    assert ls.processing_target([_fl("human", F), _fl("human", F, A)]) == (F, A)


def test_target_several_families_wait_for_review_c():
    assert ls.processing_target([_fl("human", F), _fl("human", G)]) is None
    # Even when one of them names a config: a versus video.
    assert ls.processing_target([_fl("human", F, A), _fl("human", G)]) is None


def test_target_no_links_or_several_configs_is_none():
    assert ls.processing_target([]) is None
    assert ls.processing_target([_fl("human", F, A), _fl("human", F, B)]) is None
