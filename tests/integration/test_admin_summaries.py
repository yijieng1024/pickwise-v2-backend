"""
GET /admin/dashboard/summary, the extended GET /reviews/pipeline-status, the
review pause reason, and laptops.updated_at.
"""
from datetime import datetime, timedelta, timezone


from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import LaptopEmbedding, LaptopPickScore, LaptopStatus
from tests.integration.conftest import make_laptop
from tests.integration.test_review_foundation import _link, _review  # noqa: F401

DASHBOARD = "/api/v2/admin/dashboard/summary"
PIPELINE = "/api/v2/reviews/pipeline-status"
_VEC = [0.01] * 768


def _family(session, brand, name):
    fam = LaptopFamily(brand_id=brand.id, name=name, family_key=name.lower())
    session.add(fam)
    session.commit()
    return fam


def _chunk(session, review, family, laptop=None, scope="family", topic="battery", start=0):
    from app.reviews.models import LaptopReviewChunk

    session.add(LaptopReviewChunk(
        laptop_id=laptop.id if laptop else None, family_id=family.id, scope=scope,
        video_id=review.video_id, channel_name="Chan", timestamp_start_seconds=start,
        timestamp_end_seconds=start + 45, chunk_text="ok", embedding=_VEC,
        sentiment_tag="neutral", topic=topic,
    ))
    session.commit()


# --- Dashboard -------------------------------------------------------------

def test_awaiting_price_is_inactive_with_price_zero_only(admin_client, session, brand):
    session.add_all([
        make_laptop(brand.id, status=LaptopStatus.INACTIVE.value, price_rm=0),
        make_laptop(brand.id, status=LaptopStatus.ACTIVE.value, price_rm=0),  # demoted to inactive by the write event
        make_laptop(brand.id, status=LaptopStatus.INACTIVE.value, price_rm=3999),  # delisted, not awaiting a price
        make_laptop(brand.id, status=LaptopStatus.SUSPENDED.value, price_rm=0),  # retired
    ])
    session.commit()

    body = admin_client.get(DASHBOARD).json()

    assert body["attention"]["awaiting_price"] == 2
    assert body["catalog"]["inactive"] == 3 and body["catalog"]["suspended"] == 1


def test_shares_count_active_laptops_only(admin_client, session, brand):
    active = make_laptop(brand.id)
    suspended = make_laptop(brand.id, status=LaptopStatus.SUSPENDED.value)
    session.add_all([active, suspended])
    session.commit()
    for laptop in (active, suspended):
        session.add(LaptopEmbedding(laptop_id=laptop.id, embedding=_VEC))
        session.add(LaptopPickScore(laptop_id=laptop.id, use_case="gaming", score=None,
                                    flags={"gpu_benchmark_unresolved": True}))
    session.commit()

    body = admin_client.get(DASHBOARD).json()

    assert body["catalog"]["active"] == 1
    assert body["catalog"]["active_with_embedding"] == 1
    assert body["catalog"]["active_with_pickscore"] == 1
    assert body["attention"]["pickscore"] == {"missing": 0, "withheld": 1, "unverified": 1}


def test_review_coverage_follows_the_details_page_tiers(admin_client, session, brand):
    tuf = _family(session, brand, "TUF F16")
    zen = _family(session, brand, "Zenbook 14")
    rog = _family(session, brand, "ROG Strix")
    own, sibling = make_laptop(brand.id, family_id=tuf.id), make_laptop(brand.id, family_id=tuf.id)
    family_wide = make_laptop(brand.id, family_id=zen.id)
    price_only = make_laptop(brand.id, family_id=rog.id)
    nothing = make_laptop(brand.id)
    session.add_all([own, sibling, family_wide, price_only, nothing])
    session.commit()
    review = _review(session)
    _chunk(session, review, tuf, own, scope="config", start=0)       # own -> tier 1; sibling -> tier 2
    _chunk(session, review, zen, None, scope="family", start=45)     # family-scope -> tier 1
    _chunk(session, review, rog, price_only, scope="config", topic="price", start=90)  # hidden

    dash = admin_client.get(DASHBOARD).json()
    pipe = admin_client.get(PIPELINE).json()

    assert dash["catalog"]["active_with_reviews"] == 3
    assert pipe["coverage"]["active_laptops_with_reviews"] == 3
    assert (pipe["coverage"]["families_with_active"], pipe["coverage"]["families_with_reviews"]) == (3, 2)


def test_freshness_dates(admin_client, session, brand):
    laptop = make_laptop(brand.id)
    session.add(laptop)
    session.commit()

    fresh = admin_client.get(DASHBOARD).json()["freshness"]

    assert fresh["passmark_refreshed_at"] is None  # until step 10 records a refresh
    assert fresh["pickscore_recalculated_at"] is None
    assert datetime.fromisoformat(fresh["catalog_changed_at"]) >= laptop.created_at.replace(tzinfo=timezone.utc) - timedelta(seconds=5)
    assert fresh["price_checked_missing"] == 1
    assert [b["source"] for b in fresh["scrape_by_brand"] if b["brand_id"] == str(brand.id)] == ["scrape"]


def test_pico_counts_are_rolling_windows(admin_client, session, admin_user):
    from app.agent.monitoring_models import AgentRunLog
    from app.rag.models import Conversation

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    convs = [Conversation(user_id=admin_user.id, title=f"c{i}") for i in range(2)]
    session.add_all(convs)
    session.commit()
    for conv, age, status, fallback in (
        (convs[0], timedelta(hours=2), "error", True),
        (convs[1], timedelta(days=3), "success", False),
        (convs[1], timedelta(days=9), "success", False),  # outside both windows
    ):
        session.add(AgentRunLog(
            conversation_id=conv.id, user_id=admin_user.id, user_message="q", reply_text="a",
            status=status, latency_ms=10, fallback_triggered=fallback, tool_calls=[],
            created_at=now - age,
        ))
    session.commit()

    pico = admin_client.get(DASHBOARD).json()["pico"]

    assert (pico["conversations_24h"], pico["conversations_7d"]) == (1, 2)
    assert (pico["runs_24h"], pico["errors_24h"], pico["fallbacks_24h"]) == (1, 1, 1)
    assert pico["runs_7d"] == 2


# --- Pipeline status -------------------------------------------------------

def test_every_funnel_count_opens_its_list(admin_client, session, brand):
    fam = _family(session, brand, "TUF F16")
    laptop = make_laptop(brand.id, family_id=fam.id)
    session.add(laptop)
    session.commit()
    _review(session, status="rejected", segments=0, failure_reason="no_track")
    _review(session, status="rejected", segments=0, failure_reason="ip_blocked")
    _review(session, status="pending", segments=0, transcript_attempts=0)
    _review(session, status="pending")                                     # unlinked
    waiting = _review(session)                                             # linked, never processed
    _link(session, waiting, fam, laptop)
    shown = _review(session, segments=3)                                   # one window, processed
    _link(session, shown, fam, laptop)
    _chunk(session, shown, fam, laptop, scope="config")
    _review(session, status="irrelevant")                                  # not in the funnel

    funnel = admin_client.get(PIPELINE).json()["funnel"]

    def total(query):
        return admin_client.get(f"/api/v2/reviews/raw?{query}&limit=1").json()["total"]

    for step in funnel:
        assert total(f"step={step['step']}") == step["count"], step["step"]
        for reason, count in step["stuck"].items():
            assert total(f"stuck={reason}") == count, reason
    counts = {s["step"]: s for s in funnel}
    assert [counts[s]["count"] for s in ("found", "transcript", "linked", "processed", "shown")] == [6, 3, 2, 1, 1]
    assert counts["transcript"]["stuck"] == {
        "transcript_terminal": 1, "transcript_retryable": 1, "transcript_deferred": 1, "transcript_unknown": 0,
    }
    assert counts["processed"]["stuck"]["never_processed"] == 1
    # count(previous step) = count(step) + sum(stuck)
    for prev, step in zip(funnel, funnel[1:]):
        assert prev["count"] == step["count"] + sum(step["stuck"].values())


def test_unknown_funnel_filter_is_400(admin_client):
    assert admin_client.get("/api/v2/reviews/raw?stuck=nope").status_code == 400
    assert admin_client.get("/api/v2/reviews/raw?step=nope").status_code == 400


def test_pipeline_status_quota_and_no_aggregate(admin_client, session):
    from app.common.llm_usage import LLMFeature, LLMUsageLog

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add_all([
        LLMUsageLog(feature=LLMFeature.REVIEW_CHUNK, model="gemma-4-31b-it", created_at=now),
        LLMUsageLog(feature=LLMFeature.REVIEW_CHUNK, model="gemma-4-31b-it", created_at=now),
        LLMUsageLog(feature=LLMFeature.REVIEW_CHUNK, model="mystery-model", created_at=now),
        LLMUsageLog(feature=LLMFeature.EMBEDDING, model="gemini-embedding-2", created_at=now),
        LLMUsageLog(feature=LLMFeature.REVIEW_CHUNK, model="gemma-4-31b-it", created_at=now - timedelta(days=2)),
    ])
    session.commit()

    body = admin_client.get(PIPELINE).json()

    assert "aggregate" not in body
    models = {m["model"]: m for m in body["quota"]["models"]}
    assert (models["gemma-4-31b-it"]["calls_today"], models["gemma-4-31b-it"]["limit_rpd"]) == (2, 14400)
    assert models["gemma-4-31b-it"]["remaining"] == 14398
    assert (models["mystery-model"]["limit_rpd"], models["mystery-model"]["remaining"]) == (None, None)
    assert body["quota"]["embedding"]["calls_today"] == 1
    assert body["quota"]["embedding"]["limit_rpd"] == 1000


def test_pause_reason_round_trip(admin_client):
    url = "/api/v2/admin/settings/review-pause-reason"

    set_res = admin_client.put(url, json={"reason": "  Waiting for the C2 accuracy check  "})
    assert set_res.status_code == 200 and set_res.json()["reason"] == "Waiting for the C2 accuracy check"
    process = admin_client.get(PIPELINE).json()["process"]
    assert process["pause_reason"] == "Waiting for the C2 accuracy check"
    assert admin_client.get(DASHBOARD).json()["attention"]["review_processing"]["reason"] == process["pause_reason"]

    assert admin_client.put(url, json={"reason": "x" * 501}).status_code == 400
    assert admin_client.put(url, json={"reason": None}).json() == {"reason": None, "updated_at": None}
    assert admin_client.get(PIPELINE).json()["process"]["pause_reason"] is None


def test_recent_runs_list_review_jobs_only(admin_client, session):
    from app.common.job_model import BackgroundJob, JobStatus, JobType

    session.add_all([
        BackgroundJob(job_type=JobType.REVIEW_RETRY_TRANSCRIPTS, status=JobStatus.COMPLETED, failed_count=2),
        BackgroundJob(job_type=JobType.GENERATE_PICK_SCORES, status=JobStatus.COMPLETED),
    ])
    session.commit()

    runs = admin_client.get(PIPELINE).json()["recent_runs"]

    assert [(r["job_type"], r["failed"]) for r in runs] == [(JobType.REVIEW_RETRY_TRANSCRIPTS, 2)]


# --- laptops.updated_at ------------------------------------------------------

def test_an_admin_edit_bumps_updated_at(admin_client, session, brand):
    laptop = make_laptop(brand.id)
    session.add(laptop)
    session.commit()
    before = laptop.updated_at

    res = admin_client.put(f"/api/v2/laptops/{laptop.id}", json={"product_name": "Renamed"})
    assert res.status_code == 200

    session.expire_all()
    assert session.get(type(laptop), laptop.id).updated_at > before


def test_updated_at_is_not_writable_through_the_api():
    from app.laptops.laptop_models import LaptopCreate, LaptopUpdate

    assert "updated_at" not in LaptopCreate.model_fields and "updated_at" not in LaptopUpdate.model_fields
