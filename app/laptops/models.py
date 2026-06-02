import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

# Base model for laptops, not tied to any specific database table
class LaptopBase(SQLModel):
    # Part 1: Core Specifications
    brand: str
    model_code: str = Field(unique=True, index=True)
    product_name: str
    price_rm: float
    cpu_benchmark: int
    gpu_benchmark: int
    ram_gb: int
    ssd_gb: int
    weight_kg: float
    battery_wh: int
    display_size_inch: float
    display_refresh_rate_hz: Optional[int] = None
    release_year: Optional[int] = None
    
    # Part 2: AI-Enhanced Features
    ai_ready: bool = Field(default=False)
    microsoft_office: bool = Field(default=False)
    os: Optional[str] = None # e.g., "Windows 11", "macOS"
    gpu_brand: Optional[str] = None # e.g., "NVIDIA", "AMD"
    processor_brand: Optional[str] = None # e.g., "Intel", "Apple"

    # Part 3: Original Specs & Image
    raw_specs: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    image_url: Optional[str] = None


# db tbl model for laptops, inherits from both DeclarativeBase and LaptopBase
class Laptop(LaptopBase, table=True):
    __tablename__ = "laptops" # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# API interface model for reading laptop data
class LaptopRead(LaptopBase):
    id: uuid.UUID
    created_at: datetime

# API request model for creating a new laptop entry
class LaptopCreate(LaptopBase):
    pass

# AI Vector Embedding
class LaptopEmbedding(SQLModel, table=True):
    __tablename__ = "laptop_embeddings" # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", unique=True)
    embedding: Any = Field(sa_column=Column(Vector(768)))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class LaptopUpdate(SQLModel):
    brand: Optional[str] = None
    model_code: Optional[str] = None
    product_name: Optional[str] = None
    price_rm: Optional[float] = None
    cpu_benchmark: Optional[int] = None
    gpu_benchmark: Optional[int] = None
    ram_gb: Optional[int] = None
    ssd_gb: Optional[int] = None
    weight_kg: Optional[float] = None
    battery_wh: Optional[int] = None
    display_size_inch: Optional[float] = None
    display_refresh_rate_hz: Optional[int] = None
    release_year: Optional[int] = None
    ai_ready: Optional[bool] = None
    microsoft_office: Optional[bool] = None
    os: Optional[str] = None
    gpu_brand: Optional[str] = None
    processor_brand: Optional[str] = None
    raw_specs: Optional[Dict[str, Any]] = None
    image_url: Optional[str] = None