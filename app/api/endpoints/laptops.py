# app/api/endpoints/laptops.py
from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from typing import List

from app.database import get_session
from app.models import Laptop, LaptopCreate, LaptopRead

router = APIRouter()

@router.post("/", response_model=LaptopRead)
def create_laptop(laptop: LaptopCreate, session: Session = Depends(get_session)):
    db_laptop = Laptop.model_validate(laptop)
    session.add(db_laptop)
    session.commit()
    session.refresh(db_laptop)
    return db_laptop

@router.get("/", response_model=List[LaptopRead])
def get_all_laptops(offset: int = 0, limit: int = 100, session: Session = Depends(get_session)):
    laptops = session.exec(select(Laptop).offset(offset).limit(limit)).all()
    return laptops