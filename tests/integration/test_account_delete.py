"""
DELETE /auth/me. Every FK to users is NO ACTION, so a missed table is an
IntegrityError 500, and a too-wide delete takes another user's rows.
"""
import uuid

import pytest
from sqlmodel import select

from app.agent.monitoring_models import AgentModelCall, AgentRunLog
from app.common.job_model import BackgroundJob
from app.common.llm_usage import LLMUsageLog
from app.laptops.price_guide_model import PriceGuide
from app.rag.models import Conversation, ConversationLaptop, Message, PipelineEvalLog
from app.saved.models import SavedLaptop
from app.users.admin_router import LAST_ADMIN_MESSAGE
from app.users.avatar_model import UserAvatar
from app.users.background_model import UserBackground
from app.users.models import LaptopUserPreference, User
from app.users.router import USERNAME_CONFIRM_MESSAGE

DELETED = (AgentModelCall, AgentRunLog, PipelineEvalLog, Message, ConversationLaptop, Conversation,
           LaptopUserPreference, UserBackground, SavedLaptop, UserAvatar)


def _user(session, role="user") -> User:
    name = f"{role}-{uuid.uuid4().hex[:8]}"
    user = User(username=name, email=f"{name}@example.invalid", hashed_password="x",
                status="active", role=role)
    session.add(user)
    session.flush()
    return user


def _seed(session, user, laptop) -> None:
    """One row in every table that points at a user, directly or through a conversation."""
    conv = Conversation(user_id=user.id, title="t")
    session.add(conv)
    session.flush()
    run = AgentRunLog(conversation_id=conv.id, user_id=user.id, user_message="hi", reply_text="r",
                      status="success", latency_ms=1, tool_calls=[])
    session.add(run)
    session.flush()
    session.add_all([
        AgentModelCall(run_id=run.id, seq=1, step=1, model="m", chain_position=0, is_fallback=False,
                       status="answered", started_at=run.created_at),
        Message(conversation_id=conv.id, role="user", content="hi"),
        ConversationLaptop(conversation_id=conv.id, laptop_id=laptop.id),
        PipelineEvalLog(user_id=user.id, query="q", gate_status="pass"),
        LLMUsageLog(feature="agent", model="m", user_id=user.id),
        LaptopUserPreference(user_id=user.id),
        UserBackground(user_id=user.id),
        SavedLaptop(user_id=user.id, laptop_id=laptop.id),
        UserAvatar(user_id=user.id, data=b"x", content_type="image/png"),
    ])


def _delete(client, username, **kwargs):
    # TestClient.delete() takes no body.
    return client.request("DELETE", "/api/v2/auth/me", json={"username": username}, **kwargs)


def _count(session, model) -> int:
    return len(session.exec(select(model)).all())


def test_deleting_an_account_removes_its_rows_and_only_its_rows(
    api_client, api_user, session, active_and_suspended
):
    laptop = active_and_suspended[0]
    other = _user(session)
    _seed(session, api_user, laptop)
    _seed(session, other, laptop)
    session.commit()
    user_id, other_id = api_user.id, other.id

    response = _delete(api_client, api_user.username)

    assert response.status_code == 204, response.text
    session.expire_all()
    assert session.get(User, user_id) is None
    assert session.get(User, other_id) is not None
    for model in DELETED:
        assert _count(session, model) == 1, model.__name__  # the other user's row
    # Usage counts stay, for the quota and cost numbers, without the user.
    assert sorted(str(r.user_id) for r in session.exec(select(LLMUsageLog)).all()) == sorted(
        ["None", str(other_id)]
    )


def test_an_admin_with_jobs_and_guides_can_delete_their_account(admin_client, admin_user, session):
    _user(session, role="admin")  # so admin_user is not the last one
    job = BackgroundJob(job_type="t", created_by=admin_user.id)
    guide = PriceGuide(kind="asus", filename="g.pdf", pdf_sha256=uuid.uuid4().hex, row_count=0,
                       uploaded_by=admin_user.id)
    session.add_all([job, guide])
    session.commit()
    admin_id, job_id, guide_id = admin_user.id, job.id, guide.id

    response = _delete(admin_client, admin_user.username)

    assert response.status_code == 204, response.text
    session.expire_all()
    assert session.get(User, admin_id) is None
    assert session.get(BackgroundJob, job_id).created_by is None
    assert session.get(PriceGuide, guide_id).uploaded_by is None


def test_the_last_active_admin_cannot_delete_their_account(admin_client, admin_user, session):
    response = _delete(admin_client, admin_user.username)

    assert response.status_code == 400
    assert response.json()["detail"] == LAST_ADMIN_MESSAGE
    assert session.get(User, admin_user.id) is not None


@pytest.mark.parametrize("request_kwargs", [
    {},                                  # no body
    {"json": {}},                        # no username
    {"json": {"username": None}},
    {"json": {"username": "someone-else"}},
    {"json": {"username": ""}},
], ids=["no-body", "no-field", "null", "wrong", "empty"])
def test_a_wrong_or_missing_username_deletes_nothing(api_client, api_user, session, request_kwargs):
    session.add(UserBackground(user_id=api_user.id))
    session.commit()

    response = api_client.request("DELETE", "/api/v2/auth/me", **request_kwargs)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == USERNAME_CONFIRM_MESSAGE
    session.expire_all()
    assert session.get(User, api_user.id) is not None
    assert _count(session, UserBackground) == 1


def test_another_users_username_is_wrong_even_for_an_admin(admin_client, admin_user, session):
    other = _user(session, role="admin")
    session.commit()

    response = _delete(admin_client, other.username)

    assert response.status_code == 400
    assert response.json()["detail"] == USERNAME_CONFIRM_MESSAGE
    assert session.get(User, other.id) is not None
