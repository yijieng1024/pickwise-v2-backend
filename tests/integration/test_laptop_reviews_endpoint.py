"""
GET /laptops/{id}/reviews (Review B3): the public details-page view, computed
from chunks on every request, following the display rule -- this laptop's own
plus family-scope chunks, else other configurations (named), else none.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

_VEC = [0.01] * 768


@pytest.fixture
def world(session, brand):
    from app.laptops.family_model import LaptopFamily

    def family(name):
        f = LaptopFamily(brand_id=brand.id, name=name, family_key=name.lower())
        session.add(f)
        session.commit()
        return f

    tuf, rog = family("TUF F16"), family("ROG Strix")
    a = make_laptop(brand.id, product_name="TUF F16 (RTX 4060)", family_id=tuf.id)
    b = make_laptop(brand.id, product_name="TUF F16 (RTX 5070)", family_id=tuf.id)
    g1 = make_laptop(brand.id, product_name="Strix (RTX 4070)", family_id=rog.id)
    g2 = make_laptop(brand.id, product_name="Strix (RTX 5080)", family_id=rog.id)
    alone = make_laptop(brand.id, product_name="No Family 14", family_id=None)
    session.add_all([a, b, g1, g2, alone])
    session.commit()
    return SimpleNamespace(tuf=tuf, rog=rog, a=a, b=b, g1=g1, g2=g2, alone=alone)


def _review(session, video_id, title):
    from app.reviews.models import RawYoutubeReview, YoutubeChannel

    if not session.get(YoutubeChannel, uuid.UUID(int=1)):
        # Committed on its own: there is no relationship between the two
        # models, so one flush may insert the review before its channel.
        session.add(YoutubeChannel(id=uuid.UUID(int=1), channel_id="UC1", channel_name="Chan",
                                    channel_img_url="https://yt3.ggpht.com/chan"))
        session.commit()
    session.add(RawYoutubeReview(video_id=video_id, channel_id="UC1", video_title=title, status="matched"))
    session.commit()


def _chunk(session, family, laptop, text, sentiment="strength", video="v1", start=0):
    from app.reviews.models import LaptopReviewChunk

    session.add(LaptopReviewChunk(
        family_id=family.id, laptop_id=laptop.id if laptop else None,
        scope="config" if laptop else "family", video_id=video, channel_name="Chan",
        timestamp_start_seconds=start, timestamp_end_seconds=start + 45,
        chunk_text=text, embedding=_VEC, sentiment_tag=sentiment,
    ))
    session.commit()


def _get(session, laptop):
    from app.laptops import laptop_router

    return laptop_router.get_laptop_reviews(laptop.id, session=session)


def test_own_and_family_scope_points_with_labels(session, world):
    _review(session, "v1", "TUF F16 review")
    _chunk(session, world.tuf, world.a, "Runs cool.", video="v1", start=0)
    _chunk(session, world.tuf, None, "Hinge is solid.", video="v2", start=0)
    _chunk(session, world.tuf, world.a, "Fans are loud.", sentiment="weakness", video="v1", start=45)
    _chunk(session, world.tuf, world.a, "Just facts.", sentiment="neutral", video="v1", start=90)
    _chunk(session, world.tuf, world.b, "The 5070 hits 90 FPS.", video="v3")  # sibling: not mixed in
    _chunk(session, world.rog, world.g1, "Other family.", video="v4")

    out = _get(session, world.a)

    assert out["source"] == "this_laptop" and out["review_count"] == 2
    by_text = {p["text"]: p for p in out["strengths"] + out["weaknesses"]}
    assert set(by_text) == {"Runs cool.", "Hinge is solid.", "Fans are loud."}  # no neutral
    assert by_text["Runs cool."]["applies_to"] == "this_configuration"
    assert by_text["Runs cool."]["video_title"] == "TUF F16 review"
    assert by_text["Runs cool."]["url"] == "https://youtube.com/watch?v=v1&t=0"
    assert by_text["Runs cool."]["channel_img_url"] == "https://yt3.ggpht.com/chan"
    assert by_text["Hinge is solid."]["applies_to"] == "configuration_not_stated"
    assert by_text["Hinge is solid."]["tested_on_name"] is None
    assert by_text["Fans are loud."]["sentiment"] == "weakness"


def test_other_configurations_when_this_laptop_has_none(session, world):
    _chunk(session, world.rog, world.g1, "Fans are loud.", sentiment="weakness")

    out = _get(session, world.g2)

    assert out["source"] == "other_configurations" and out["strengths"] == []
    [p] = out["weaknesses"]
    assert p["applies_to"] == "other_configuration"
    assert (p["tested_on_laptop_id"], p["tested_on_name"]) == (world.g1.id, "Strix (RTX 4070)")
    assert p["channel_img_url"] is None  # no registered channel: the frontend shows the initial


def test_no_reviews_yet(session, world):
    for laptop in (world.alone, world.b):
        out = _get(session, laptop)
        assert out == {"laptop_id": laptop.id, "source": "none", "review_count": 0,
                       "strengths": [], "weaknesses": []}


def test_top_five_distinct_per_sentiment(session, world):
    _chunk(session, world.tuf, world.a, "Same point.", start=0)
    _chunk(session, world.tuf, world.a, "same point. ", start=45)
    for i in range(6):
        _chunk(session, world.tuf, world.a, f"Point {i}.", start=100 + 45 * i)

    out = _get(session, world.a)

    texts = [p["text"].strip().casefold() for p in out["strengths"]]
    assert len(texts) == 5 and len(set(texts)) == 5


def test_unknown_laptop_is_404(session):
    from app.laptops import laptop_router

    with pytest.raises(HTTPException) as exc:
        laptop_router.get_laptop_reviews(uuid.uuid4(), session=session)
    assert exc.value.status_code == 404


def test_route_is_public_and_matches_the_response_model(api_client, session, world):
    """Through the app: the payload validates against LaptopReviewsRead, and
    the route declares no auth dependency (api_client overrides the user, so
    the request alone cannot show that)."""
    from app.main import app

    _chunk(session, world.tuf, None, "Hinge is solid.")

    resp = api_client.get(f"/api/v2/laptops/{world.a.id}/reviews")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "this_laptop"
    assert body["strengths"][0]["applies_to"] == "configuration_not_stated"

    [route] = [r for r in app.routes if getattr(r, "path", "") == "/api/v2/laptops/{laptop_id}/reviews"]
    deps = {d.call.__name__ for d in route.dependant.dependencies}
    assert deps == {"get_session"}
