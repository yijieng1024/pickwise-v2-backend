"""
10c step (f)'s --apply path (app/scripts/retire_bto_10c.py) against the test database:
the 10 rows go to suspended with their derived data kept, and a3205f6d's chunks and
links move to the standard config with the same chip.
"""
import json
import uuid

import pytest
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.reviews.link_model import ReviewLaptopLink
from app.reviews.models import LaptopReviewChunk, RawYoutubeReview, YoutubeChannel
from app.scripts import retire_bto_10c as script
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

M5PRO = dict(processor_model="Apple M5 Pro (18-core)", gpu_model="20-core GPU", cpu_cores=18, gpu_cores=20)


def _uid(prefix):
    return uuid.UUID(f"{prefix}-0000-4000-8000-{uuid.uuid4().hex[:12]}")


@pytest.fixture
def macs(session):
    apple = LaptopBrand(name="Apple", base_scrape_url="https://www.apple.com/my/mac/")
    session.add(apple)
    session.flush()
    fam = LaptopFamily(brand_id=apple.id, name="MacBook Pro 16", family_key="macbook pro 16")
    session.add(fam)
    session.flush()
    rows = {p: make_laptop(apple.id, id=_uid(p), family_id=fam.id, status=s, price_rm=15199.0 if s == "active" else 0.0,
                           **(M5PRO if p == script.REVIEWED else {}))
            for p, s in script.EXPECTED.items()}
    std = make_laptop(apple.id, id=_uid(script.SURVEYED_TARGET), family_id=fam.id, part_numbers=["MX2T3ZP/A"],
                      price_rm=12499.0, **M5PRO)
    session.add_all([*rows.values(), std])
    session.flush()
    session.add(LaptopPriceHistory(laptop_id=rows[script.REVIEWED].id, price_rm=15199.0))
    session.add(YoutubeChannel(channel_id="UC1", channel_name="Chan"))
    session.commit()
    review = RawYoutubeReview(video_id="v1", channel_id="UC1", video_title="MacBook Pro 16 review", status="matched")
    session.add(review)
    session.flush()
    session.add(ReviewLaptopLink(raw_review_id=review.id, family_id=fam.id, laptop_id=rows[script.REVIEWED].id,
                                 match_source="auto"))
    for start in (0, 45, 90):
        session.add(LaptopReviewChunk(family_id=fam.id, laptop_id=rows[script.REVIEWED].id, scope="config",
                                      video_id="v1", channel_name="Chan", timestamp_start_seconds=start,
                                      timestamp_end_seconds=start + 45, chunk_text=f"claim {start}",
                                      embedding=[0.1] * 768, sentiment_tag="strength"))
    session.commit()
    return rows, std


def test_apply_suspends_the_ten_and_moves_the_reviews(session, macs, tmp_path):
    rows, std = macs
    result = script.run(session, write=True, backups=tmp_path)

    session.expire_all()
    assert {session.get(Laptop, l.id).status for l in rows.values()} == {"suspended"}
    assert session.get(Laptop, std.id).status == "active"
    assert session.exec(select(LaptopPriceHistory).where(
        LaptopPriceHistory.laptop_id == rows[script.REVIEWED].id)).all(), "price history stays"
    chunks = session.exec(select(LaptopReviewChunk)).all()
    links = session.exec(select(ReviewLaptopLink)).all()
    assert {c.laptop_id for c in chunks} == {std.id} and {l.laptop_id for l in links} == {std.id}
    assert (result["repoint"]["chunks"], result["repoint"]["links"]) == (3, 1)

    pre, post = sorted(tmp_path.glob("*.json"))
    assert {r["status"] for r in json.loads(pre.read_text())["laptops"]} == {"active", "inactive"}
    assert {r["laptop_id"] for r in json.loads(post.read_text())["laptop_review_chunks"]} == {str(std.id)}

    with pytest.raises(script.Refused, match="status is 'suspended'"):
        script.run(session, write=True, backups=tmp_path)
