from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select as sa_select
from sqlmodel import Session, select
from app.database import get_session
from app.laptops.laptop_models import (
    Laptop, LaptopRead, LaptopCreate, LaptopUpdate, LaptopPriceHistory, LaptopPriceHistoryRead,
    LaptopEmbedding, HybridSearchRequest, LaptopSearchResult
)
from app.laptops.brand_model import LaptopBrand
from app.embeddings.service import embed_text
from app.scraper.models import RawScrapLaptop
from typing import List
from uuid import UUID
from app.users.auth import get_current_admin

router = APIRouter(prefix="/laptops", tags=["Laptops"])

@router.post("/", response_model=LaptopRead, status_code=201, dependencies=[Depends(get_current_admin)])
def create_laptop(laptop: LaptopCreate, session: Session = Depends(get_session)):
    db_laptop = Laptop.model_validate(laptop)
    session.add(db_laptop)
    session.commit()
    session.refresh(db_laptop)

    session.add(LaptopPriceHistory(laptop_id=db_laptop.id, price_rm=db_laptop.price_rm))
    session.commit()

    return db_laptop

@router.get("/", response_model=List[LaptopRead])
def list_laptops(session: Session = Depends(get_session)):
    return session.exec(select(Laptop)).all()

@router.post("/hybrid-search", response_model=List[LaptopSearchResult])
def hybrid_search(request: HybridSearchRequest, session: Session = Depends(get_session)):
    query_vector = embed_text(request.query)
    distance = LaptopEmbedding.embedding.cosine_distance(query_vector)

    statement = (
        sa_select(Laptop, LaptopBrand.name, distance.label("distance")) # type: ignore
        .join(LaptopEmbedding, LaptopEmbedding.laptop_id == Laptop.id)
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
    )

    if request.budget_max is not None:
        statement = statement.where(Laptop.price_rm <= request.budget_max)
    if request.brand is not None:
        statement = statement.where(LaptopBrand.name.ilike(request.brand))

    statement = statement.order_by(distance.asc()).limit(request.top_k)

    rows = session.execute(statement).all()

    return [
        LaptopSearchResult(
            laptop_id=laptop.id,
            product_name=f"{brand_name} {laptop.product_name}",
            price_rm=laptop.price_rm,
            similarity_score=round(1 - row_distance, 4),
        )
        for laptop, brand_name, row_distance in rows
    ]

@router.get("/raw-scrap-laptops", dependencies=[Depends(get_current_admin)])
def list_raw_scrap_laptops(
    offset: int = 0, 
    limit: int = 50, 
    session: Session = Depends(get_session)
) -> List[RawScrapLaptop]:

    statement = select(RawScrapLaptop).order_by(RawScrapLaptop.created_at.desc()).offset(offset).limit(limit) # type: ignore
    scrap_laptops = session.exec(statement).all()
    
    return list(scrap_laptops)

@router.get("/{laptop_id}", response_model=LaptopRead)
def get_laptop(laptop_id: UUID, session: Session = Depends(get_session)):
    laptop = session.get(Laptop, laptop_id)
    if not laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")
    return laptop

@router.put("/{laptop_id}", response_model=LaptopRead, dependencies=[Depends(get_current_admin)])
def update_laptop(
    laptop_id: UUID, 
    laptop_update: LaptopUpdate, 
    session: Session = Depends(get_session)
):
    db_laptop = session.get(Laptop, laptop_id)
    if not db_laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")
    
    update_data = laptop_update.model_dump(exclude_unset=True)
    price_changed = "price_rm" in update_data and update_data["price_rm"] != db_laptop.price_rm

    for key, value in update_data.items():
        setattr(db_laptop, key, value)

    session.add(db_laptop)
    session.commit()
    session.refresh(db_laptop)

    if price_changed:
        session.add(LaptopPriceHistory(laptop_id=db_laptop.id, price_rm=db_laptop.price_rm))
        session.commit()

    return db_laptop

@router.get("/{laptop_id}/price-history", response_model=List[LaptopPriceHistoryRead])
def get_laptop_price_history(laptop_id: UUID, session: Session = Depends(get_session)):
    db_laptop = session.get(Laptop, laptop_id)
    if not db_laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")

    statement = (
        select(LaptopPriceHistory)
        .where(LaptopPriceHistory.laptop_id == laptop_id)
        .order_by(LaptopPriceHistory.recorded_at.asc())  # type: ignore
    )
    return session.exec(statement).all()

@router.delete("/{laptop_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_laptop(laptop_id: UUID, session: Session = Depends(get_session)):
    db_laptop = session.get(Laptop, laptop_id)
    if not db_laptop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Laptop not found")
    
    session.delete(db_laptop)
    session.commit()
    
    return None

# @router.post("/bulk", response_model=List[LaptopRead], status_code=status.HTTP_201_CREATED)
# def bulk_create_laptops(
#     laptops_in: List[LaptopCreate], 
#     session: Session = Depends(get_session)
# ):
#     db_laptops = []
    
#     for laptop_data in laptops_in:
#         # Check if model_code already exists to prevent integrity errors during bulk insert
#         existing = session.exec(select(Laptop).where(Laptop.model_code == laptop_data.model_code)).first()
#         if existing:
#             continue 
            
#         laptop = Laptop.model_validate(laptop_data)
#         # Note: Depending on your exact Laptop model setup, ensure ID and created_at are generated
#         session.add(laptop)
#         db_laptops.append(laptop)
        
#     session.commit()
    
#     for laptop in db_laptops:
#         session.refresh(laptop)
        
#     return db_laptops