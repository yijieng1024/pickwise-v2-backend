import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.database import get_session
from app.laptops.laptop_models import Laptop, LaptopRead
from app.saved.models import SavedLaptop
from app.users.auth import get_current_user
from app.users.models import User

router = APIRouter(prefix="/saved", tags=["Saved Laptops"])


@router.get("/", response_model=list[LaptopRead])
def list_saved(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """The user's saved laptops as full laptop records, newest saved first."""
    return session.exec(
        select(Laptop)
        .join(SavedLaptop, SavedLaptop.laptop_id == Laptop.id)  # type: ignore[arg-type]
        .where(SavedLaptop.user_id == current_user.id)
        .order_by(SavedLaptop.created_at.desc())  # type: ignore[attr-defined]
    ).all()


@router.get("/ids")
def list_saved_ids(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[uuid.UUID]:
    """Just the saved laptop ids — lightweight heart-state lookup."""
    return list(session.exec(
        select(SavedLaptop.laptop_id).where(SavedLaptop.user_id == current_user.id)
    ).all())


@router.put("/{laptop_id}", status_code=status.HTTP_204_NO_CONTENT)
def save_laptop(
    laptop_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Save a laptop (idempotent — saving twice is a no-op)."""
    if not session.get(Laptop, laptop_id):
        raise HTTPException(status_code=404, detail="Laptop not found")

    existing = session.exec(
        select(SavedLaptop).where(
            SavedLaptop.user_id == current_user.id,
            SavedLaptop.laptop_id == laptop_id,
        )
    ).first()
    if not existing:
        session.add(SavedLaptop(user_id=current_user.id, laptop_id=laptop_id))
        session.commit()


@router.delete("/{laptop_id}", status_code=status.HTTP_204_NO_CONTENT)
def unsave_laptop(
    laptop_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Remove a saved laptop (idempotent — removing a non-saved one is a no-op)."""
    existing = session.exec(
        select(SavedLaptop).where(
            SavedLaptop.user_id == current_user.id,
            SavedLaptop.laptop_id == laptop_id,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
