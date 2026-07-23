from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.database import get_session
from app.users.auth import get_current_admin
from app.users.models import User, UserAdminRead, UserListResponse
from app.users.schema import UserRoleUpdate, UserStatusUpdate

router = APIRouter(prefix="/users", tags=["Admin - Users"])


@router.get("", response_model=UserListResponse, dependencies=[Depends(get_current_admin)])
def list_users(
    search: Optional[str] = Query(default=None, description="Matches username or email (case-insensitive)"),
    role: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status", description="active, inactive, or suspended"),
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    """List users with optional search/role/status filters (admin only)."""
    query = select(User)

    if search:
        pattern = f"%{search}%"
        query = query.where(or_(User.username.ilike(pattern), User.email.ilike(pattern)))
    if role:
        query = query.where(User.role == role)
    if status_filter:
        query = query.where(User.status == status_filter)

    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    items = session.exec(
        query.order_by(User.created_at.desc()).offset(skip).limit(limit)
    ).all()

    return UserListResponse(total=total, items=list(items))


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
