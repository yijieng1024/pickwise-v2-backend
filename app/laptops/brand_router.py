from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from app.database import get_session
from app.laptops.models import Laptop, LaptopBrand, BrandCreate, BrandRead, BrandUpdate
from typing import List
from uuid import UUID

from app.users.auth import get_current_admin

router = APIRouter(prefix="/brands", tags=["Laptop Brands"])


@router.post("", response_model=BrandRead, dependencies=[Depends(get_current_admin)], status_code=201)
def create_brand(brand: BrandCreate, session: Session = Depends(get_session)):
    """Create a new laptop brand."""
    # Check if brand name already exists
    existing_brand = session.exec(
        select(LaptopBrand).where(LaptopBrand.name == brand.name)
    ).first()

    if existing_brand:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Brand with name '{brand.name}' already exists",
        )

    db_brand = LaptopBrand.model_validate(brand)
    session.add(db_brand)
    session.commit()
    session.refresh(db_brand)
    return db_brand


@router.get("", response_model=List[BrandRead])
def list_brands(
    offset: int = 0,
    limit: int = 100,
    is_active: bool = None, # type: ignore
    session: Session = Depends(get_session),
):
    """List all laptop brands with optional filtering."""
    query = select(LaptopBrand)

    if is_active is not None:
        query = query.where(LaptopBrand.is_active == is_active)

    query = query.offset(offset).limit(limit)
    brands = session.exec(query).all()
    return brands


@router.get("/{brand_id}", response_model=BrandRead)
def get_brand(brand_id: UUID, session: Session = Depends(get_session)):
    """Get a specific brand by ID."""
    brand = session.get(LaptopBrand, brand_id)
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found"
        )
    return brand


@router.put("/{brand_id}", response_model=BrandRead, dependencies=[Depends(get_current_admin)])
def update_brand(
    brand_id: UUID, brand_update: BrandUpdate, session: Session = Depends(get_session)
):
    """Update an existing brand."""
    db_brand = session.get(LaptopBrand, brand_id)
    if not db_brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found"
        )

    # Check if new name already exists (if name is being updated)
    if brand_update.name and brand_update.name != db_brand.name:
        existing_brand = session.exec(
            select(LaptopBrand).where(LaptopBrand.name == brand_update.name)
        ).first()
        if existing_brand:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Brand with name '{brand_update.name}' already exists",
            )

    update_data = brand_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_brand, key, value)

    session.add(db_brand)
    session.commit()
    session.refresh(db_brand)
    return db_brand


@router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_brand(brand_id: UUID, session: Session = Depends(get_session)):
    """Delete a brand."""
    db_brand = session.get(LaptopBrand, brand_id)
    if not db_brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found"
        )

    # Check if brand has associated laptops
    associated_laptops = session.exec(
        select(Laptop).where(Laptop.brand_id == brand_id)
    ).all()

    if associated_laptops:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete brand with {len(associated_laptops)} associated laptops",
        )

    session.delete(db_brand)
    session.commit()
    return None
