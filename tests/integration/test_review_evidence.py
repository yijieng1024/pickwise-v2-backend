"""
get_review_evidence reads by family (Review B2): this laptop's chunks plus its
family's family-scope chunks, sibling configurations only as a labelled
fallback, a distance cutoff against filler, and deduplication. Integration tier
because the ranking is pgvector's cosine distance.
"""
import math
import uuid
from types import SimpleNamespace

import pytest

from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

_DIM = 768


def _vec(distance: float) -> list[float]:
    """A unit vector at exactly `distance` (1 - cos) from the query vector e0."""
    theta = math.acos(1 - distance)
    v = [0.0] * _DIM
    v[0], v[1] = math.cos(theta), math.sin(theta)
    return v


_QUERY = _vec(0.0)


@pytest.fixture
def world(session, brand):
    from app.laptops.family_model import LaptopFamily

    def family(name):
        f = LaptopFamily(brand_id=brand.id, name=name, family_key=name.lower())
        session.add(f)
        session.commit()
        return f

    tuf, rog, lone = family("TUF F16"), family("ROG Strix"), family("Lone")
    a = make_laptop(brand.id, product_name="TUF F16 (RTX 4060)", family_id=tuf.id)
    b = make_laptop(brand.id, product_name="TUF F16 (RTX 5070)", family_id=tuf.id)
    g1 = make_laptop(brand.id, product_name="Strix (RTX 4070)", family_id=rog.id)
    g2 = make_laptop(brand.id, product_name="Strix (RTX 5080)", family_id=rog.id)
    empty = make_laptop(brand.id, product_name="Lone 14", family_id=lone.id)
    session.add_all([a, b, g1, g2, empty])
    session.commit()
    return SimpleNamespace(tuf=tuf, rog=rog, a=a, b=b, g1=g1, g2=g2, empty=empty)


def _chunk(session, family, laptop, text, distance, scope=None, start=0, video="v1"):
    from app.reviews.models import LaptopReviewChunk

    session.add(LaptopReviewChunk(
        family_id=family.id, laptop_id=laptop.id if laptop else None,
        scope=scope or ("config" if laptop else "family"),
        video_id=video, channel_name="Chan", timestamp_start_seconds=start,
        timestamp_end_seconds=start + 45, chunk_text=text,
        embedding=_vec(distance), sentiment_tag="strength",
    ))
    session.commit()


@pytest.fixture
def evidence(session, monkeypatch):
    from app.agent.tools import laptop_tools

    monkeypatch.setattr(laptop_tools, "engine", session.connection())
    monkeypatch.setattr(laptop_tools, "embed_text", lambda q: _QUERY)
    monkeypatch.setattr(laptop_tools, "settings", SimpleNamespace(review_evidence_max_distance=0.44))
    return lambda laptop: laptop_tools.get_review_evidence.invoke(
        {"laptop_id": str(laptop.id), "query_context": "thermals"})


def test_own_and_family_scope_chunks_are_returned_with_labels(session, world, evidence):
    _chunk(session, world.tuf, world.a, "Own config runs cool.", 0.10, start=0)
    _chunk(session, world.tuf, None, "The hinge is solid.", 0.20, start=45)
    _chunk(session, world.tuf, world.b, "The 5070 hits 90 FPS.", 0.05, video="v2")  # sibling config
    _chunk(session, world.rog, world.g1, "Other family entirely.", 0.01, video="v3")
    _chunk(session, world.tuf, world.a, "Far away claim.", 0.60, start=90)

    out = evidence(world.a)

    assert "Own config runs cool." in out and "Applies to: this configuration" in out
    assert "The hinge is solid." in out and "configuration not stated" in out
    # A sibling's config-scope claim is a fallback only; another family never.
    assert "90 FPS" not in out and "Other family" not in out
    assert "Far away" not in out  # beyond the 0.44 cutoff
    assert "NOTE:" in out  # the family-scope line carries the instruction


def test_only_own_config_needs_no_note(session, world, evidence):
    _chunk(session, world.tuf, world.a, "Own config runs cool.", 0.10)
    out = evidence(world.a)
    assert "this configuration" in out and "NOTE:" not in out


def test_siblings_are_used_when_own_evidence_is_not_relevant(session, world, evidence):
    _chunk(session, world.tuf, world.a, "About the keyboard.", 0.50)          # own, beyond cutoff
    _chunk(session, world.tuf, None, "About the speakers.", 0.55, start=45)  # family, beyond cutoff
    _chunk(session, world.tuf, world.b, "The 5070 runs cool.", 0.10, video="v2")

    out = evidence(world.a)

    assert "The 5070 runs cool." in out
    assert "tested on TUF F16 (RTX 5070)" in out and "NOTE:" in out
    # Never mixed: the out-of-cutoff tier-1 chunks stay out.
    assert "keyboard" not in out and "speakers" not in out


def test_siblings_are_a_labelled_fallback_when_nothing_else_exists(session, world, evidence):
    _chunk(session, world.rog, world.g1, "Fans are loud under load.", 0.10)

    out = evidence(world.g2)

    assert "Fans are loud under load." in out
    assert "tested on Strix (RTX 4070)" in out and "NOTE:" in out


def test_duplicates_collapse_and_results_are_capped_at_three(session, world, evidence):
    _chunk(session, world.tuf, world.a, "Runs cool.", 0.10, start=0)
    _chunk(session, world.tuf, world.a, "runs cool. ", 0.11, start=45)          # same text
    _chunk(session, world.tuf, world.a, "Same window, other text.", 0.12, start=0)  # same window
    for i in range(4):
        _chunk(session, world.tuf, world.a, f"Point {i}.", 0.2 + i / 100, start=100 + 45 * i)

    out = evidence(world.a)

    assert out.lower().count("runs cool.") == 1
    assert "Same window" not in out
    assert out.count("[STRENGTH]") == 3


def test_nothing_relevant_and_nothing_at_all_are_different_answers(session, world, evidence):
    assert evidence(world.empty) == "No review evidence found for this laptop yet."

    _chunk(session, world.tuf, world.a, "About the keyboard.", 0.50)
    _chunk(session, world.tuf, world.b, "Sibling, also off-topic.", 0.60, video="v2")
    out = evidence(world.a)
    assert "none of it is relevant" in out and "[STRENGTH]" not in out
