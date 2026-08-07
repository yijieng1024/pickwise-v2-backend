from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.laptops.brand_model import LaptopBrand, BrandRead, BrandCreate, BrandUpdate
from app.common.filter_service import apply_filters, filter_query
from typing import List, Optional
from uuid import UUID

from app.users.auth import get_current_admin

router = APIRouter(prefix="/brands", tags=["Laptop Brands"])

# Allow-list for the filter params — see app.common.filter_service.
BRAND_FILTERABLE_COLUMNS = {"is_active": LaptopBrand.is_active}


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
    # Was `is_active: bool = None` with a type: ignore, which documented itself
    # in OpenAPI as a plain bool and gave generated clients no way to express
    # "either". Optional[bool] defaulting to None says what it means.
    is_active: Optional[bool] = filter_query("true, false, or omit for both"),
    session: Session = Depends(get_session),
):
    """List all laptop brands with optional filtering."""
    query = select(LaptopBrand)
    query = apply_filters(query, {"is_active": is_active}, BRAND_FILTERABLE_COLUMNS)

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
