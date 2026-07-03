from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from app.database import get_session
from app.taxonomy.product_type_model import (
    ProductType,
    ProductTypeRead,
    ProductTypeCreate,
    ProductTypeUpdate,
)
from app.users.questionnaire_model import QuestionnaireQuestion
from typing import List
from uuid import UUID

from app.users.auth import get_current_admin

router = APIRouter(prefix="/product-types", tags=["Product Types"])


@router.post("", response_model=ProductTypeRead, dependencies=[Depends(get_current_admin)], status_code=201)
def create_product_type(product_type: ProductTypeCreate, session: Session = Depends(get_session)):
    """Create a new product type."""
    existing = session.exec(
        select(ProductType).where(ProductType.name == product_type.name)
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Product type with name '{product_type.name}' already exists",
        )

    db_product_type = ProductType.model_validate(product_type)
    session.add(db_product_type)
    session.commit()
    session.refresh(db_product_type)
    return db_product_type


@router.get("", response_model=List[ProductTypeRead])
def list_product_types(
    offset: int = 0,
    limit: int = 100,
    is_active: bool = None,  # type: ignore
    session: Session = Depends(get_session),
):
    """List all product types with optional filtering."""
    query = select(ProductType)

    if is_active is not None:
        query = query.where(ProductType.is_active == is_active)

    query = query.offset(offset).limit(limit)
    product_types = session.exec(query).all()
    return product_types


@router.get("/{product_type_id}", response_model=ProductTypeRead)
def get_product_type(product_type_id: UUID, session: Session = Depends(get_session)):
    """Get a specific product type by ID."""
    product_type = session.get(ProductType, product_type_id)
    if not product_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product type not found"
        )
    return product_type


@router.put("/{product_type_id}", response_model=ProductTypeRead, dependencies=[Depends(get_current_admin)])
def update_product_type(
    product_type_id: UUID, product_type_update: ProductTypeUpdate, session: Session = Depends(get_session)
):
    """Update an existing product type."""
    db_product_type = session.get(ProductType, product_type_id)
    if not db_product_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product type not found"
        )

    if product_type_update.name and product_type_update.name != db_product_type.name:
        existing = session.exec(
            select(ProductType).where(ProductType.name == product_type_update.name)
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product type with name '{product_type_update.name}' already exists",
            )

    update_data = product_type_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_product_type, key, value)

    session.add(db_product_type)
    session.commit()
    session.refresh(db_product_type)
    return db_product_type


@router.delete("/{product_type_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_product_type(product_type_id: UUID, session: Session = Depends(get_session)):
    """Delete a product type."""
    db_product_type = session.get(ProductType, product_type_id)
    if not db_product_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product type not found"
        )

    associated_questions = session.exec(
        select(QuestionnaireQuestion).where(QuestionnaireQuestion.product_type_id == product_type_id)
    ).all()

    if associated_questions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete product type with {len(associated_questions)} associated questionnaire questions",
        )

    session.delete(db_product_type)
    session.commit()
    return None
