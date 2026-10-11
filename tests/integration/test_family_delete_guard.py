"""
DELETE /families/{id} must refuse while a review link points at the family.

review_laptop_link.family_id is NOT NULL with a NO ACTION FK, so the route's
"release members to null" default has nowhere to put a link: before the guard
this was a raw IntegrityError 500 (hit on 2026-10-11 while merging families).
Integration tier for the same reason as test_delete_guard.py — the thing being
tested is the database constraint the guard gets in front of.
"""

import pytest
from fastapi import HTTPException

from app.laptops import family_router
from app.laptops.family_model import LaptopFamily
from app.reviews.link_model import ReviewLaptopLink
from app.reviews.models import RawYoutubeReview, YoutubeChannel
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


@pytest.fixture
def family(session, brand):
    row = LaptopFamily(brand_id=brand.id, name="ROG Strix 18", family_key="rog strix g18")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture
def review(session):
    session.add(YoutubeChannel(channel_id="UCtest", channel_name="Test Channel"))
    session.commit()
    row = RawYoutubeReview(video_id="vid1", channel_id="UCtest", video_title="SCAR 18 review")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_a_family_with_a_review_link_is_refused_with_409(session, family, review, brand):
    member = make_laptop(brand.id, family_id=family.id)
    session.add(member)
    session.add(ReviewLaptopLink(raw_review_id=review.id, family_id=family.id, match_source="human"))
    session.commit()

    with pytest.raises(HTTPException) as exc:
        family_router.delete_family(family.id, session)

    assert exc.value.status_code == 409
    assert "1 review link" in exc.value.detail
    assert "ROG Strix 18" in exc.value.detail
    # Refused means nothing was written: the family survives and its member
    # was not released to unassigned on the way to the refusal.
    session.expire_all()
    assert session.get(LaptopFamily, family.id) is not None
    assert session.get(type(member), member.id).family_id == family.id


def test_a_family_without_review_links_still_deletes_and_releases(session, family, brand):
    """The guard must not turn every delete into a refusal — releasing members
    is the second half of every merge."""
    member = make_laptop(brand.id, family_id=family.id)
    session.add(member)
    session.commit()

    family_router.delete_family(family.id, session)

    session.expire_all()
    assert session.get(LaptopFamily, family.id) is None
    assert session.get(type(member), member.id).family_id is None
