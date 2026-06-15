from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, col
from typing import List
import uuid
from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.laptops.customization_model import CustomizationUpdate, CustomizationBulkCreate, CustomizationRead, LaptopCustomization
from app.users.auth import get_current_admin 
# from app.schemas import CustomizationBulkCreate, CustomizationRead, CustomizationUpdate

router = APIRouter(prefix="/customizations", tags=["Laptop Customizations"])

@router.post("/", response_model=List[CustomizationRead], dependencies=[Depends(get_current_admin)])
def create_customizations_for_laptops(
    request: CustomizationBulkCreate, 
    session: Session = Depends(get_session)
):
    """
    BULK CREATE: Assigns a customization to one or multiple laptops at the same time.
    """
    created_customizations = []
    
    # Verify all laptops exist before inserting anything
    laptops = session.exec(
        select(Laptop).where(col(Laptop.id).in_(request.laptop_ids))
    ).all()
    
    if len(laptops) != len(request.laptop_ids):
        raise HTTPException(
            status_code=404, 
            detail="One or more Laptop IDs provided do not exist in the database."
        )

    # Create a distinct row for each laptop
    for laptop_id in request.laptop_ids:
        new_customization = LaptopCustomization(
            laptop_id=laptop_id,
            category=request.category,
            option_name=request.option_name,
            price_add_rm=request.price_add_rm,
            dependency_note=request.dependency_note
        )
        session.add(new_customization)
        created_customizations.append(new_customization)

    session.commit()
    
    # Refresh to get the generated UUIDs
    for custom in created_customizations:
        session.refresh(custom)
        
    return created_customizations


@router.get("/laptop/{laptop_id}", response_model=List[CustomizationRead], dependencies=[Depends(get_current_admin)])
def get_customizations_by_laptop(
    laptop_id: uuid.UUID, 
    session: Session = Depends(get_session)
):
    """
    READ: Get all available upgrades for a specific base laptop model.
    """
    customizations = session.exec(
        select(LaptopCustomization).where(LaptopCustomization.laptop_id == laptop_id)
    ).all()
    
    return customizations


@router.patch("/{customization_id}", response_model=CustomizationRead, dependencies=[Depends(get_current_admin)])
def update_customization(
    customization_id: uuid.UUID, 
    update_data: CustomizationUpdate, 
    session: Session = Depends(get_session)
):
    """
    UPDATE: Edit an existing customization (e.g., Apple changed the price).
    """
    db_customization = session.get(LaptopCustomization, customization_id)
    if not db_customization:
        raise HTTPException(status_code=404, detail="Customization not found")

    update_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(db_customization, key, value)

    session.add(db_customization)
    session.commit()
    session.refresh(db_customization)
    
    return db_customization


@router.delete("/{customization_id}", dependencies=[Depends(get_current_admin)])
def delete_customization(
    customization_id: uuid.UUID, 
    session: Session = Depends(get_session)
):
    """
    DELETE: Remove a customization option.
    """
    db_customization = session.get(LaptopCustomization, customization_id)
    if not db_customization:
        raise HTTPException(status_code=404, detail="Customization not found")

    session.delete(db_customization)
    session.commit()
    
    return {"message": "Customization deleted successfully"}