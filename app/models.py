import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

# User Module
class User(SQLModel, table=True):
    __tablename__ = "users"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="user")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# Laptop Module
# Base type：Pydantic Validation + SQLModel
class LaptopBase(SQLModel):
    brand: str
    model_code: str = Field(unique=True, index=True)
    product_name: str
    price_rm: float
    
    # PickScore Indicators
    cpu_benchmark: int
    gpu_benchmark: int
    ram_gb: int
    weight_kg: float
    battery_wh: int
    
    # JSONB to store raw scraped specs
    raw_specs: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    image_url: Optional[str] = None

# DB Model with table=True
class Laptop(LaptopBase, table=True):
    __tablename__ = "laptops"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# API return to frontend data format
class LaptopRead(LaptopBase):
    id: uuid.UUID
    created_at: datetime

# API 接收前端创建请求的数据格式
class LaptopCreate(LaptopBase):
    pass

# pgvector embedding table for RAG
class LaptopEmbedding(SQLModel, table=True):
    __tablename__ = "laptop_embeddings"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # 关联到具体的 Laptop
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", unique=True)
    
    # 【亮点】这里定义了 768 维的向量字段（适配 Google Gemini 模型的标准嵌入维度）
    embedding: Any = Field(sa_column=Column(Vector(768)))
    
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))