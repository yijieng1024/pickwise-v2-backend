from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select, col
from typing import List
import uuid
from app.database import get_session
from app.laptops.laptop_models import Laptop
from app.laptops.customization_model import CustomizationUpdate, CustomizationBulkCreate, CustomizationRead, LaptopCustomization, LaptopCustomizationSummary, PatternMatchLaptop
from app.laptops.customization_schema import CustomizationBulkCreateByPattern
from app.taxonomy.category_model import Category
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

    # Checked up front so a bad tag id is a readable 404 rather than an
    # IntegrityError surfacing as a 500 at commit time.
    if not session.get(Category, request.category_id):
        raise HTTPException(status_code=404, detail="Category not found.")

    # Create a distinct row for each laptop
    for laptop_id in request.laptop_ids:
        new_customization = LaptopCustomization(
            laptop_id=laptop_id,
            category_id=request.category_id,
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


def _laptops_matching_pattern(session: Session, target_pattern: str) -> List[Laptop]:
    """
    The laptops that POST /bulk-by-pattern would write to.

    Shared with the preview endpoint deliberately. `.contains()` compiles to a
    case-sensitive `LIKE '%…%'`, and the admin UI has to show exactly which
    laptops the write will hit — it is bulk and has no undo. Re-implementing
    this predicate anywhere else is how a preview silently stops matching the
    write it claims to preview.
    """
    statement = select(Laptop).where(col(Laptop.model_code).contains(target_pattern))
    return list(session.exec(statement).all())


@router.get("/bulk-by-pattern/preview", response_model=List[PatternMatchLaptop], dependencies=[Depends(get_current_admin)])
def preview_customizations_by_pattern(
    target_pattern: str = Query(
        min_length=1, description="Case-sensitive substring matched against model_code"
    ),
    session: Session = Depends(get_session),
):
    """
    Which laptops POST /bulk-by-pattern would assign an option to, writing
    nothing. Returns an empty list rather than a 404 when nothing matches —
    "no matches yet" is the normal state while an admin is still typing.
    """
    return _laptops_matching_pattern(session, target_pattern)


@router.post("/bulk-by-pattern", response_model=List[CustomizationRead], dependencies=[Depends(get_current_admin)])
def create_customizations_by_pattern(
    request: CustomizationBulkCreateByPattern,
    session: Session = Depends(get_session)
):
    """
    BULK CREATE BY PATTERN: Assigns a customization to all laptops where model_code contains the target_pattern.
    e.g., target_pattern="m5-max" will add this upgrade to all M5 Max laptops automatically.
    """
    created_customizations = []

    # 1. Find all laptops matching the keyword in their model_code
    matching_laptops = _laptops_matching_pattern(session, request.target_pattern)

    if not matching_laptops:
        raise HTTPException(
            status_code=404,
            detail=f"No laptops found matching pattern: '{request.target_pattern}'"
        )

    # Checked up front so a bad tag id is a readable 404 rather than an
    # IntegrityError surfacing as a 500 at commit time.
    if not session.get(Category, request.category_id):
        raise HTTPException(status_code=404, detail="Category not found.")

    # 2. Create a distinct customization row for each matching laptop
    for laptop in matching_laptops:
        new_customization = LaptopCustomization(
            laptop_id=laptop.id,
            category_id=request.category_id,
            option_name=request.option_name,
            price_add_rm=request.price_add_rm,
            dependency_note=request.dependency_note
        )
        session.add(new_customization)
        created_customizations.append(new_customization)

    session.commit()
    
    # 3. Refresh to get the generated UUIDs
    for custom in created_customizations:
        session.refresh(custom)
        
    return created_customizations


@router.get("/laptops-summary", response_model=List[LaptopCustomizationSummary], dependencies=[Depends(get_current_admin)])
def get_laptops_with_customizations(session: Session = Depends(get_session)):
    """
    READ: Lists every laptop that has at least one customization, with a count.

    Powers the admin customizations picker so admins see which laptops
    actually have upgrade options configured instead of searching blind.
    """
    # Joined rather than returning bare ids: the picker has to label every
    # row, and without the name it would need one /laptops/{id} call each.
    rows = session.execute(
        select(
            LaptopCustomization.laptop_id,
            Laptop.product_name,
            Laptop.model_code,
            func.count(LaptopCustomization.id).label("customization_count"),
        )
        .join(Laptop, col(Laptop.id) == col(LaptopCustomization.laptop_id))
        .group_by(
            LaptopCustomization.laptop_id,
            Laptop.product_name,
            Laptop.model_code,
        )
        .order_by(func.count(LaptopCustomization.id).desc())
    ).all()

    return [
        LaptopCustomizationSummary(
            laptop_id=row.laptop_id,
            product_name=row.product_name,
            model_code=row.model_code,
            customization_count=row.customization_count,
        )
        for row in rows
    ]


@router.get("/laptop/{laptop_id}", response_model=List[CustomizationRead], dependencies=[Depends(get_current_admin)])
def get_customizations_by_laptop(
    laptop_id: uuid.UUID, 
    session: Session = Depends(get_session)
):
    """
    READ: Get all available upgrades for a specific base laptop model.
    """
    customizations = session.exec(
        select(LaptopCustomization).where(col(LaptopCustomization.laptop_id) == laptop_id)
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