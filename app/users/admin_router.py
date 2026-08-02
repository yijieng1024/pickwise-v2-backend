from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

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


@router.get("", response_model=Page[UserAdminRead], dependencies=[Depends(get_current_admin)])
def list_users(
    search: Optional[str] = search_query("Matches username or email (case-insensitive)"),
    role: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status", description="active, inactive, or suspended"),
    sort_by: Optional[str] = Query(default=None, description="One of: username, created_at"),
    sort_dir: SortDirection = sort_dir_query(default=SortDirection.desc),
    pagination: PaginationParams = Depends(),
    session: Session = Depends(get_session),
):
    """List users with optional search/role/status filters (admin only)."""
    statement = select(User)

    statement = apply_search(statement, search, [User.username, User.email])
    if role:
        statement = statement.where(User.role == role)
    if status_filter:
        statement = statement.where(User.status == status_filter)

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
    if user_id == current_admin.id and payload.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot change your own role.",
        )

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

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
    if user_id == current_admin.id and payload.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot change your own account status.",
        )

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.status = payload.status
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
