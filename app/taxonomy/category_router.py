from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from app.database import get_session
from app.taxonomy.category_model import (
    Category,
    CategoryRead,
    CategoryCreate,
    CategoryUpdate,
)
from app.laptops.laptop_category_model import LaptopCategory
from app.laptops.customization_model import LaptopCustomization
from typing import List
from uuid import UUID

from app.users.auth import get_current_admin

router = APIRouter(prefix="/categories", tags=["Categories"])


@router.post("", response_model=CategoryRead, dependencies=[Depends(get_current_admin)], status_code=201)
def create_category(category: CategoryCreate, session: Session = Depends(get_session)):
    """Create a new category (tag)."""
    existing = session.exec(
        select(Category).where(Category.name == category.name)
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Category with name '{category.name}' already exists",
        )

    db_category = Category.model_validate(category)
    session.add(db_category)
    session.commit()
    session.refresh(db_category)
    return db_category


@router.get("", response_model=List[CategoryRead])
def list_categories(
    offset: int = 0,
    limit: int = 100,
    is_active: bool = None,  # type: ignore
    session: Session = Depends(get_session),
):
    """List all categories with optional filtering."""
    query = select(Category)

    if is_active is not None:
        query = query.where(Category.is_active == is_active)

    query = query.offset(offset).limit(limit)
    categories = session.exec(query).all()
    return categories


@router.get("/{category_id}", response_model=CategoryRead)
def get_category(category_id: UUID, session: Session = Depends(get_session)):
    """Get a specific category by ID."""
    category = session.get(Category, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found"
        )
    return category


@router.put("/{category_id}", response_model=CategoryRead, dependencies=[Depends(get_current_admin)])
def update_category(
    category_id: UUID, category_update: CategoryUpdate, session: Session = Depends(get_session)
):
    """Update an existing category."""
    db_category = session.get(Category, category_id)
    if not db_category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found"
        )

    if category_update.name and category_update.name != db_category.name:
        existing = session.exec(
            select(Category).where(Category.name == category_update.name)
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Category with name '{category_update.name}' already exists",
            )

    update_data = category_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_category, key, value)

    session.add(db_category)
    session.commit()
    session.refresh(db_category)
    return db_category


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_category(category_id: UUID, session: Session = Depends(get_session)):
    """Delete a category."""
    db_category = session.get(Category, category_id)
    if not db_category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found"
        )

    associated_laptops = session.exec(
        select(LaptopCategory).where(LaptopCategory.category_id == category_id)
    ).all()
    associated_customizations = session.exec(
        select(LaptopCustomization).where(LaptopCustomization.category_id == category_id)
    ).all()

    if associated_laptops or associated_customizations:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete category with {len(associated_laptops)} tagged laptops "
                f"and {len(associated_customizations)} associated customizations"
            ),
        )

    session.delete(db_category)
    session.commit()
    return None
