from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from sqlmodel import Session, select
from typing import List, Optional
from uuid import UUID

from app.benchmark.cpu_scraper import run_cpu_list_scraper
from app.benchmark.gpu_scraper import run_gpu_list_scraper
from app.common.pagination_service import PaginationParams, paginate
from app.common.search_service import apply_search, search_query
from app.common.sorting_service import SortDirection, apply_sort, sort_dir_query
from app.database import get_session
from app.users.auth import get_current_admin
from app.benchmark.model import (
    CPUBenchmark, CPUBenchmarkCreate, CPUBenchmarkUpdate, CPUBenchmarkRead,
    GPUBenchmark, GPUBenchmarkCreate, GPUBenchmarkUpdate, GPUBenchmarkRead
)

router = APIRouter(prefix="/benchmarks", tags=["Benchmarks"])

# Allow-lists for ?sort_by= — see app.common.sorting_service.
CPU_SORTABLE_COLUMNS = {"cpu_name": CPUBenchmark.cpu_name, "cpu_mark": CPUBenchmark.cpu_mark}
GPU_SORTABLE_COLUMNS = {"gpu_name": GPUBenchmark.gpu_name, "gpu_mark": GPUBenchmark.gpu_mark}

# CPU BENCHMARK ENDPOINTS

@router.post("/cpu", response_model=CPUBenchmarkRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(get_current_admin)])
def create_cpu_benchmark(
    data: CPUBenchmarkCreate, 
    db: Session = Depends(get_session),
    admin: dict = Depends(get_current_admin)
):
    existing = db.exec(select(CPUBenchmark).where(CPUBenchmark.cpu_name == data.cpu_name)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="CPU benchmark already exists")
    
    db_cpu = CPUBenchmark.model_validate(data)
    db.add(db_cpu)
    db.commit()
    db.refresh(db_cpu)
    return db_cpu

@router.get("/cpu", response_model=List[CPUBenchmarkRead])
def list_cpu_benchmarks(
    search: Optional[str] = search_query("Matches CPU name"),
    sort_by: Optional[str] = Query(default=None, description="One of: cpu_name, cpu_mark"),
    sort_dir: SortDirection = sort_dir_query(),
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_session),
):
    statement = select(CPUBenchmark)
    statement = apply_search(statement, search, [CPUBenchmark.cpu_name])
    statement = apply_sort(statement, sort_by, sort_dir, CPU_SORTABLE_COLUMNS, CPUBenchmark.cpu_name)
    return db.exec(paginate(statement, pagination)).all()

@router.get("/cpu/{id}", response_model=CPUBenchmarkRead)
def get_cpu_benchmark(id: UUID, db: Session = Depends(get_session)):
    db_cpu = db.get(CPUBenchmark, id)
    if not db_cpu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CPU benchmark not found")
    return db_cpu

@router.patch("/cpu/{id}", response_model=CPUBenchmarkRead, dependencies=[Depends(get_current_admin)])
def update_cpu_benchmark(
    id: UUID, 
    data: CPUBenchmarkUpdate, 
    db: Session = Depends(get_session),
    admin: dict = Depends(get_current_admin)
):
    db_cpu = db.get(CPUBenchmark, id)
    if not db_cpu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CPU benchmark not found")
    
    update_dict = data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(db_cpu, key, value)
        
    db.add(db_cpu)
    db.commit()
    db.refresh(db_cpu)
    return db_cpu

@router.delete("/cpu/{id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_cpu_benchmark(
    id: UUID, 
    db: Session = Depends(get_session),
    admin: dict = Depends(get_current_admin)
):
    db_cpu = db.get(CPUBenchmark, id)
    if not db_cpu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CPU benchmark not found")
    db.delete(db_cpu)
    db.commit()
    return

@router.post("/scrape/cpu", status_code=status.HTTP_200_OK, dependencies=[Depends(get_current_admin)])
def trigger_cpu_scraper(
    background_tasks: BackgroundTasks,
    admin: dict = Depends(get_current_admin)
):
    """
    Admin protected route to run the PassMark scraper pipeline.
    Dispatched via FastAPI BackgroundTasks to keep response execution non-blocking.
    """
    try:
        # Offload the execution to background tasks so your API worker doesn't time out
        background_tasks.add_task(run_cpu_list_scraper)
        return {"status": "success", "message": "PassMark CPU list scraper pipeline initialized in background."}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Failed to initialize scraper flow: {str(e)}"
        )
    

# GPU BENCHMARK ENDPOINTS

@router.post("/gpu", response_model=GPUBenchmarkRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(get_current_admin)])
def create_gpu_benchmark(
    data: GPUBenchmarkCreate, 
    db: Session = Depends(get_session),
    admin: dict = Depends(get_current_admin)
):
    existing = db.exec(select(GPUBenchmark).where(GPUBenchmark.gpu_name == data.gpu_name)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="GPU benchmark already exists")
    
    db_gpu = GPUBenchmark.model_validate(data)
    db.add(db_gpu)
    db.commit()
    db.refresh(db_gpu)
    return db_gpu

@router.get("/gpu", response_model=List[GPUBenchmarkRead])
def list_gpu_benchmarks(
    search: Optional[str] = search_query("Matches GPU name"),
    sort_by: Optional[str] = Query(default=None, description="One of: gpu_name, gpu_mark"),
    sort_dir: SortDirection = sort_dir_query(),
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_session),
):
    statement = select(GPUBenchmark)
    statement = apply_search(statement, search, [GPUBenchmark.gpu_name])
    statement = apply_sort(statement, sort_by, sort_dir, GPU_SORTABLE_COLUMNS, GPUBenchmark.gpu_name)
    return db.exec(paginate(statement, pagination)).all()

@router.get("/gpu/{id}", response_model=GPUBenchmarkRead)
def get_gpu_benchmark(id: UUID, db: Session = Depends(get_session)):
    db_gpu = db.get(GPUBenchmark, id)
    if not db_gpu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU benchmark not found")
    return db_gpu

@router.patch("/gpu/{id}", response_model=GPUBenchmarkRead, dependencies=[Depends(get_current_admin)])
def update_gpu_benchmark(
    id: UUID, 
    data: GPUBenchmarkUpdate, 
    db: Session = Depends(get_session),
    admin: dict = Depends(get_current_admin)
):
    db_gpu = db.get(GPUBenchmark, id)
    if not db_gpu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU benchmark not found")
    
    update_dict = data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(db_gpu, key, value)
        
    db.add(db_gpu)
    db.commit()
    db.refresh(db_gpu)
    return db_gpu

@router.delete("/gpu/{id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_current_admin)])
def delete_gpu_benchmark(
    id: UUID, 
    db: Session = Depends(get_session),
    admin: dict = Depends(get_current_admin)
):
    db_gpu = db.get(GPUBenchmark, id)
    if not db_gpu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GPU benchmark not found")
    db.delete(db_gpu)
    db.commit()
    return

@router.post("/scrape/gpu", status_code=status.HTTP_200_OK, dependencies=[Depends(get_current_admin)])
def trigger_gpu_scraper(
    background_tasks: BackgroundTasks,
    admin: dict = Depends(get_current_admin)
):
    """
    Admin protected route to run the PassMark Video Card list scraper pipeline.
    Dispatched via FastAPI BackgroundTasks to keep response execution non-blocking.
    """
    try:
        # Offload the execution to background tasks so your API worker doesn't time out
        background_tasks.add_task(run_gpu_list_scraper)
        return {"status": "success", "message": "PassMark GPU list scraper pipeline initialized in background."}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Failed to initialize scraper flow: {str(e)}"
        )