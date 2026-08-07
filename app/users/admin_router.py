from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.common.filter_service import apply_filters, filter_query
from app.common.pagination_service import Page, PaginationParams, count_total, paginate
from app.common.search_service import apply_search, search_query
from app.common.sorting_service import SortDirection, apply_sort, sort_dir_query
from app.database import get_session
from app.users.auth import get_current_admin
from app.users.models import User, UserAdminRead
from app.users.schema import UserRoleUpdate, UserStatusUpdate

router = APIRouter(prefix="/users", tags=["Admin - Users"])

# Allow-list for ?sort_by= — see app.common.sorting_service.
USER_SORTABLE_COLUMNS = {
    "username": User.username,
    "created_at": User.created_at,
}

# Allow-list for the filter params — see app.common.filter_service. Keys are the
# public names, so `status` rather than the `status_filter` argument holding it.
USER_FILTERABLE_COLUMNS = {
    "role": User.role,
    "status": User.status,
}

SELF_DEMOTION_MESSAGE = "Admins cannot demote or deactivate their own account."
LAST_ADMIN_MESSAGE = "Cannot modify or delete the last remaining active admin account."


# ---------------------------------------------------------------------------
# Lockout guards
# ---------------------------------------------------------------------------
#
# Two ways an admin can lock everyone out of the portal, both irreversible
# without direct database access:
#   1. demoting or suspending themselves while alone,
#   2. removing admin from the only remaining active admin.
#
# Both checks live here so every mutating user endpoint applies them
# identically — including a future DELETE /users/{id}, which does not exist
# yet (see `assert_can_delete_user`).


def count_active_admins(session: Session) -> int:
    """Admins who can actually sign in — role admin AND status active."""
    return len(
        session.exec(
            select(User).where(User.role == "admin", User.status == "active")
        ).all()
    )


def _is_active_admin(user: User) -> bool:
    return user.role == "admin" and user.status == "active"


def assert_not_self_lockout(
    target: User, current_admin: User, *, is_demotion: bool
) -> None:
    """Block an admin removing their own access. 403 — it is about identity."""
    if target.id == current_admin.id and is_demotion:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=SELF_DEMOTION_MESSAGE
        )


def assert_not_last_admin(session: Session, target: User) -> None:
    """
    Block removing the final active admin.

    In practice this mostly backs up the self-guard: the caller is an active
    admin themselves, so any *other* active admin means the count is already
    at least 2. It still matters if the acting account is ever changed (a
    service account, a break-glass path) or if status enforcement moves —
    this is the check that keeps the invariant true regardless.
    """
    if _is_active_admin(target) and count_active_admins(session) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=LAST_ADMIN_MESSAGE
        )


def assert_can_delete_user(session: Session, target: User, current_admin: User) -> None:
    """
    Guard for user deletion. No DELETE /users/{id} route exists today; this is
    the check it must call when one is added, so the rule is not re-derived.
    """
    assert_not_self_lockout(target, current_admin, is_demotion=True)
    assert_not_last_admin(session, target)


@router.get("", response_model=Page[UserAdminRead], dependencies=[Depends(get_current_admin)])
def list_users(
    search: Optional[str] = search_query("Matches username or email (case-insensitive)"),
    role: Optional[str] = filter_query("admin or user"),
    status_filter: Optional[str] = filter_query(
        "active, inactive, or suspended", alias="status"
    ),
    sort_by: Optional[str] = Query(default=None, description="One of: username, created_at"),
    sort_dir: SortDirection = sort_dir_query(default=SortDirection.desc),
    pagination: PaginationParams = Depends(),
    session: Session = Depends(get_session),
):
    """List users with optional search/role/status filters (admin only)."""
    statement = select(User)

    statement = apply_filters(
        statement,
        {"role": role, "status": status_filter},
        USER_FILTERABLE_COLUMNS,
    )
    statement = apply_search(statement, search, [User.username, User.email])

    total = count_total(session, statement)

    statement = apply_sort(statement, sort_by, sort_dir, USER_SORTABLE_COLUMNS, User.created_at)
    items = session.exec(paginate(statement, pagination)).all()

    return Page(items=list(items), total=total, skip=pagination.skip, limit=pagination.limit)


@router.get("/{user_id}", response_model=UserAdminRead, dependencies=[Depends(get_current_admin)])
def get_user(user_id: UUID, session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.patch("/{user_id}/role", response_model=UserAdminRead)
def update_user_role(
    user_id: UUID,
    payload: UserRoleUpdate,
    current_admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
):
    """
    Set a user's role.

    Refuses to remove the last route into the portal: an admin cannot demote
    themselves (403), and the final active admin cannot be demoted by anyone
    (400). Promotions are never blocked.
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    is_demotion = user.role == "admin" and payload.role != "admin"
    if is_demotion:
        assert_not_self_lockout(user, current_admin, is_demotion=True)
        assert_not_last_admin(session, user)

    user.role = payload.role
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.patch("/{user_id}/status", response_model=UserAdminRead)
def update_user_status(
    user_id: UUID,
    payload: UserStatusUpdate,
    current_admin: User = Depends(get_current_admin),
    session: Session = Depends(get_session),
):
    """
    Set a user's account status.

    Same lockout guards as the role endpoint: an admin cannot deactivate or
    suspend themselves (403), and the final active admin cannot be deactivated
    by anyone (400). Re-activating is never blocked.
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    is_deactivation = user.status == "active" and payload.status != "active"
    if is_deactivation:
        assert_not_self_lockout(user, current_admin, is_demotion=True)
        assert_not_last_admin(session, user)

    user.status = payload.status
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
