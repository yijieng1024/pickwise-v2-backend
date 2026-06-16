from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field
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