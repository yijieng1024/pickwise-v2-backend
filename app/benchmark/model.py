from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4
from sqlalchemy import func
from sqlmodel import Session, SQLModel, Field, select
from pydantic import BaseModel

# CPU Benchmark Schemas & Models
class CPUBenchmarkBase(SQLModel):
    cpu_name: str = Field(unique=True, index=True, nullable=False)
    cpu_mark: int = Field(default=0, nullable=False)

class CPUBenchmark(CPUBenchmarkBase, table=True):
    __tablename__ = "cpu_benchmarks" #type: ignore
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)

class CPUBenchmarkCreate(CPUBenchmarkBase):
    pass

class CPUBenchmarkUpdate(BaseModel):
    cpu_name: Optional[str] = None
    cpu_mark: Optional[int] = None

class CPUBenchmarkRead(CPUBenchmarkBase):
    id: UUID


# GPU Benchmark Schemas & Models
class GPUBenchmarkBase(SQLModel):
    gpu_name: str = Field(unique=True, index=True, nullable=False)
    gpu_mark: int = Field(default=0, nullable=False)

class GPUBenchmark(GPUBenchmarkBase, table=True):
    __tablename__ = "gpu_benchmarks" #type: ignore
    
    id: UUID = Field(default_factory=uuid4, primary_key=True)

class GPUBenchmarkCreate(GPUBenchmarkBase):
    pass

class GPUBenchmarkUpdate(BaseModel):
    gpu_name: Optional[str] = None
    gpu_mark: Optional[int] = None

class GPUBenchmarkRead(GPUBenchmarkBase):
    id: UUID


class BenchmarkRefresh(SQLModel, table=True):
    """One row per successful PassMark run, written in the same transaction as
    its upsert. The date shown is ours, not PassMark's: they update daily, and
    our marks are only as fresh as our last scrape."""

    __tablename__ = "benchmark_refreshes"  # type: ignore

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source: str = Field(index=True, max_length=10, description="'cpu' or 'gpu'")
    # Naive UTC, like the other log tables
    refreshed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    row_count: int
    source_updated_text: Optional[str] = Field(default=None, max_length=100)


def latest_refreshes(session: Session) -> dict[str, Optional[datetime]]:
    """When each list was last refreshed, as aware UTC. None if it never was."""
    rows = session.exec(
        select(BenchmarkRefresh.source, func.max(BenchmarkRefresh.refreshed_at)).group_by(BenchmarkRefresh.source)
    ).all()
    found = {source: at.replace(tzinfo=timezone.utc) for source, at in rows}
    return {"cpu": found.get("cpu"), "gpu": found.get("gpu")}
