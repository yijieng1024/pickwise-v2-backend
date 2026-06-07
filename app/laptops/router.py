from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from app.database import get_session
from app.laptops.models import Laptop, LaptopRead, LaptopCreate, LaptopUpdate, RawScrapLaptop
from typing import List
from uuid import UUID
from app.users.auth import get_current_admin

router = APIRouter(prefix="/laptops", tags=["Laptops"])

@router.post("/", response_model=LaptopRead, status_code=201)
def create_laptop(laptop: LaptopCreate, session: Session = Depends(get_session)):
    db_laptop = Laptop.model_validate(laptop)
    session.add(db_laptop)
    session.commit()
    session.refresh(db_laptop)
    return db_laptop

@router.get("/", response_model=List[LaptopRead])
def list_laptops(session: Session = Depends(get_session)):
    return session.exec(select(Laptop)).all()

@router.get("/raw-scrap-laptops", dependencies=[Depends(get_current_admin)])
def list_raw_scrap_laptops(
    offset: int = 0, 
    limit: int = 50, 
    session: Session = Depends(get_session)
) -> List[RawScrapLaptop]:
    """
    Retrieves a paginated list of raw scraped laptops for the Admin dashboard.
    """
    # Using offset and limit prevents memory crashes
    statement = select(RawScrapLaptop).order_by(RawScrapLaptop.created_at.desc()).offset(offset).limit(limit) # type: ignore
    scrap_laptops = session.exec(statement).all()
    
    return list(scrap_laptops)

@router.get("/{laptop_id}", response_model=LaptopRead)
def get_laptop(laptop_id: UUID, session: Session = Depends(get_session)):
    """Retrieve a single laptop by its ID."""
    laptop = session.get(Laptop, laptop_id)
    if not laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")
    return laptop

@router.put("/{laptop_id}", response_model=LaptopRead)
def update_laptop(
    laptop_id: UUID, 
    laptop_update: LaptopUpdate, 
    session: Session = Depends(get_session)
):
    """Update a laptop's specifications partially."""
    db_laptop = session.get(Laptop, laptop_id)
    if not db_laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")
    
    # Extract only the fields that were explicitly set in the request
    update_data = laptop_update.model_dump(exclude_unset=True)
    
    for key, value in update_data.items():
        setattr(db_laptop, key, value)
        
    session.add(db_laptop)
    session.commit()
    session.refresh(db_laptop)
    
    return db_laptop

@router.delete("/{laptop_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_laptop(laptop_id: UUID, session: Session = Depends(get_session)):
    """Remove a laptop from the database."""
    db_laptop = session.get(Laptop, laptop_id)
    if not db_laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")
    
    session.delete(db_laptop)
    session.commit()
    
    return None

@router.post("/bulk", response_model=List[LaptopRead], status_code=status.HTTP_201_CREATED)
def bulk_create_laptops(
    laptops_in: List[LaptopCreate], 
    session: Session = Depends(get_session)
):
    """Seed the database with multiple laptops at once."""
    db_laptops = []
    
    for laptop_data in laptops_in:
        # Check if model_code already exists to prevent integrity errors during bulk insert
        existing = session.exec(select(Laptop).where(Laptop.model_code == laptop_data.model_code)).first()
        if existing:
            continue 
            
        laptop = Laptop.model_validate(laptop_data)
        # Note: Depending on your exact Laptop model setup, ensure ID and created_at are generated
        session.add(laptop)
        db_laptops.append(laptop)
        
    session.commit()
    
    for laptop in db_laptops:
        session.refresh(laptop)
        
    return db_laptops