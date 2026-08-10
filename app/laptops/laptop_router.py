from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
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
from app.common.filter_service import apply_filters, apply_range, filter_query
from app.common.pagination_service import count_total
from app.common.search_service import apply_search, search_query
from app.common.sorting_service import SortDirection, apply_sort, sort_dir_query
from typing import List, Optional
from uuid import UUID
from app.users.auth import get_current_admin

router = APIRouter(prefix="/laptops", tags=["Laptops"])

# Allow-list for ?sort_by= — keeps sorting to columns that are indexed/cheap
# and meant to be exposed, per app.common.sorting_service.
LAPTOP_SORTABLE_COLUMNS = {
    "product_name": Laptop.product_name,
    "price_rm": Laptop.price_rm,
    "created_at": Laptop.created_at,
}

# Allow-list for the filter params — see app.common.filter_service. Price is a
# range rather than an equality, so it goes through apply_range and is not here.
LAPTOP_FILTERABLE_COLUMNS = {
    "brand_id": Laptop.brand_id,
    "ram_gb": Laptop.ram_gb,
    "storage_type": Laptop.storage_type,
}

RAW_SCRAP_FILTERABLE_COLUMNS = {
    "processing_status": RawScrapLaptop.processing_status,
    "brand_id": RawScrapLaptop.brand_id,
}

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
def list_laptops(
    response: Response,
    search: Optional[str] = search_query("Matches product name, model code, or brand name"),
    brand_id: Optional[UUID] = filter_query("Only laptops from this brand"),
    ram_gb: Optional[int] = filter_query("Exact RAM size in GB"),
    storage_type: Optional[str] = filter_query("e.g. SSD, HDD"),
    price_min: Optional[float] = filter_query("Lowest price in RM (inclusive)", ge=0),
    price_max: Optional[float] = filter_query("Highest price in RM (inclusive)", ge=0),
    sort_by: Optional[str] = Query(default=None, description="One of: product_name, price_rm, created_at"),
    sort_dir: SortDirection = sort_dir_query(),
    skip: Optional[int] = Query(default=None, ge=0, description="Omit to get every row (default, unpaginated)"),
    limit: Optional[int] = Query(default=None, ge=1, le=200, description="Omit to get every row (default, unpaginated)"),
    session: Session = Depends(get_session),
):
    """
    All params are optional and additive: called with no query params, this
    returns the full unpaginated/unfiltered catalog exactly as before — every
    existing caller (client-side browse/filter, admin dashboard counts) keeps
    working unchanged. Pass search/brand_id/ram_gb/storage_type/price_min/
    price_max/sort_by/sort_dir/skip/limit to opt into server-side filtering,
    sorting, and slicing.

    The body stays a bare array (unchanged contract) — the filtered row count
    travels via the `X-Total-Count` response header instead, so callers that
    want real pagination (not just a client-side slice) can read it without
    a breaking response-shape change.
    """
    # Joined so search can also match the brand name (the old client-side
    # search did this too — kept for parity now that search moved server-side).
    statement = select(Laptop).join(LaptopBrand, Laptop.brand_id == LaptopBrand.id)  # type: ignore[arg-type]
    statement = apply_filters(
        statement,
        {"brand_id": brand_id, "ram_gb": ram_gb, "storage_type": storage_type},
        LAPTOP_FILTERABLE_COLUMNS,
    )
    statement = apply_range(statement, Laptop.price_rm, price_min, price_max)  # type: ignore[arg-type]
    statement = apply_search(statement, search, [Laptop.product_name, Laptop.model_code, LaptopBrand.name])

    # After filter+search, before sort/slice — so this is the filtered total.
    response.headers["X-Total-Count"] = str(count_total(session, statement))

    statement = apply_sort(statement, sort_by, sort_dir, LAPTOP_SORTABLE_COLUMNS, Laptop.created_at)

    if skip is not None:
        statement = statement.offset(skip)
    if limit is not None:
        statement = statement.limit(limit)

    return session.exec(statement).all()

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
    response: Response,
    processing_status: Optional[str] = filter_query(
        "pending | processing | completed | failed"
    ),
    brand_id: Optional[UUID] = filter_query("Only records for this brand"),
    search: Optional[str] = search_query("Matches raw product name or source URL"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> List[RawScrapLaptop]:
    """
    The raw collected records, newest first.

    The body stays a bare array (unchanged contract); the filtered row count
    travels via `X-Total-Count`, matching GET /laptops/. Before this, callers
    had to pull the whole table just to count rows per status.
    """
    statement = select(RawScrapLaptop)
    statement = apply_filters(
        statement,
        {"processing_status": processing_status, "brand_id": brand_id},
        RAW_SCRAP_FILTERABLE_COLUMNS,
    )
    statement = apply_search(
        statement, search, [RawScrapLaptop.raw_product_name, RawScrapLaptop.source_url]
    )

    response.headers["X-Total-Count"] = str(count_total(session, statement))

    # `id` breaks ties: a bulk scrape inserts many rows within the same clock
    # tick, and ordering by created_at alone leaves their relative order
    # undefined — so a paging client can see one row twice and miss another.
    statement = (
        statement.order_by(
            RawScrapLaptop.created_at.desc(),  # type: ignore[attr-defined]
            RawScrapLaptop.id,
        )
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(statement).all())

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