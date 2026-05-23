from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.database import get_session
from app.laptops.models import Laptop, LaptopCreate, LaptopRead
from typing import List

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