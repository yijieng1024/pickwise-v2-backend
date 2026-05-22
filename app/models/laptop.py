import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

class LaptopBase(SQLModel):
    brand: str
    model_code: str = Field(unique=True, index=True)
    product_name: str
    price_rm: float
    cpu_benchmark: int
    gpu_benchmark: int
    ram_gb: int
    weight_kg: float
    battery_wh: int
    raw_specs: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    image_url: Optional[str] = None

class Laptop(LaptopBase, table=True):
    __tablename__ = "laptops"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class LaptopRead(LaptopBase):
    id: uuid.UUID
    created_at: datetime

class LaptopCreate(LaptopBase):
    pass

class LaptopEmbedding(SQLModel, table=True):
    __tablename__ = "laptop_embeddings"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", unique=True)
    embedding: Any = Field(sa_column=Column(Vector(768)))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))