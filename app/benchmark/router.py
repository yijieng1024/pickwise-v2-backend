from fastapi import APIRouter, Depends, HTTPException, Query, Response, status, BackgroundTasks
from sqlmodel import Session, select
from typing import List, Optional
from uuid import UUID

from app.benchmark.passmark_scraper import SOURCES, refresh_worker
from app.common.job_model import JobAccepted, JobType
from app.common.job_service import create_job, job_accepted, refuse_if_running, run_job
from app.common.pagination_service import PaginationParams, count_total, paginate
from app.common.search_service import apply_search, search_query
from app.common.sorting_service import SortDirection, apply_sort, sort_dir_query
from app.database import get_session
from app.users.auth import get_current_admin
from app.users.models import User
from app.benchmark.model import (
    CPUBenchmark, CPUBenchmarkCreate, CPUBenchmarkUpdate, CPUBenchmarkRead,
    GPUBenchmark, GPUBenchmarkCreate, GPUBenchmarkUpdate, GPUBenchmarkRead
)

router = APIRouter(prefix="/benchmarks", tags=["Benchmarks"])

# Allow-lists for ?sort_by= — see app.common.sorting_service.
CPU_SORTABLE_COLUMNS = {"cpu_name": CPUBenchmark.cpu_name, "cpu_mark": CPUBenchmark.cpu_mark}
GPU_SORTABLE_COLUMNS = {"gpu_name": GPUBenchmark.gpu_name, "gpu_mark": GPUBenchmark.gpu_mark}

# Measured: 8,934 rows in about 12 minutes, one upsert at a time. Only feeds the
# 202's ETA; the job's real progress comes from its row counts.
_SECONDS_PER_ROW = 0.08


def _start_refresh(
    source: str, job_type: str, background_tasks: BackgroundTasks, session: Session, admin: User
) -> JobAccepted:
    """One PassMark refresh as a background job. 409 while either list is running."""
    refuse_if_running(session, JobType.PASSMARK, "A PassMark refresh is already running.")
    job = create_job(
        session,
        job_type=job_type,
        total_count=SOURCES[source].min_rows,  # corrected once the list is fetched
        params={"source": source},
        created_by=admin.id,
        seconds_per_item=_SECONDS_PER_ROW,
    )
    background_tasks.add_task(run_job, job.id, refresh_worker(source))
    return job_accepted(job, message=f"Refreshing PassMark {source.upper()} marks in the background.")


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
    response: Response,
    search: Optional[str] = search_query("Matches CPU name"),
    sort_by: Optional[str] = Query(default=None, description="One of: cpu_name, cpu_mark"),
    sort_dir: SortDirection = sort_dir_query(),
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_session),
):
    statement = select(CPUBenchmark)
    statement = apply_search(statement, search, [CPUBenchmark.cpu_name])
    response.headers["X-Total-Count"] = str(count_total(db, statement))
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

@router.post("/scrape/cpu", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def trigger_cpu_scraper(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    admin: User = Depends(get_current_admin),
) -> JobAccepted:
    """
    Refresh the PassMark CPU list. **Returns 202 with a job.** Poll `poll_url`; the
    finished job's `result` holds `rows_written`. A list below the row floor fails
    the job with the reason. 409 while either PassMark refresh is running.
    """
    return _start_refresh("cpu", JobType.PASSMARK_CPU, background_tasks, session, admin)

    

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
    response: Response,
    search: Optional[str] = search_query("Matches GPU name"),
    sort_by: Optional[str] = Query(default=None, description="One of: gpu_name, gpu_mark"),
    sort_dir: SortDirection = sort_dir_query(),
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_session),
):
    statement = select(GPUBenchmark)
    statement = apply_search(statement, search, [GPUBenchmark.gpu_name])
    response.headers["X-Total-Count"] = str(count_total(db, statement))
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

@router.post("/scrape/gpu", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def trigger_gpu_scraper(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    admin: User = Depends(get_current_admin),
) -> JobAccepted:
    """
    Refresh the PassMark Video Card list. **Returns 202 with a job.** Poll `poll_url`; the
    finished job's `result` holds `rows_written`. A list below the row floor fails
    the job with the reason. 409 while either PassMark refresh is running.
    """
    return _start_refresh("gpu", JobType.PASSMARK_GPU, background_tasks, session, admin)
