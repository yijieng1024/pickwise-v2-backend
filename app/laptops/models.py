import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from sqlmodel import Relationship, SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSON, JSONB
from pgvector.sqlalchemy import Vector


class LaptopBrand(SQLModel, table=True):
    __tablename__ = "laptop_brands"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(unique=True, index=True)
    base_scrape_url: str
    icons_url: Optional[str] = Field(nullable=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Brand Schema Models for API
class BrandBase(SQLModel):
    name: str
    base_scrape_url: str
    icons_url: Optional[str] = None
    is_active: bool = Field(default=True)


class BrandCreate(BrandBase):
    pass


class BrandUpdate(SQLModel):
    name: Optional[str] = None
    base_scrape_url: Optional[str] = None
    icons_url: Optional[str] = None
    is_active: Optional[bool] = None


class BrandRead(BrandBase):
    id: uuid.UUID
    created_at: datetime

class LaptopCustomization(SQLModel, table=True):
    __tablename__ = "laptop_customizations" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    
    # Category of the upgrade: "RAM", "Storage", "Processor", "Power Adapter"
    category: str 
    
    # The exact name: "24GB Unified Memory" or "1TB SSD"
    option_name: str 
    
    # The additional cost: 850.00
    # Note: Store the ADDED cost, not the total cost. 
    price_add_rm: float 
    
    # If upgrading this requires another upgrade (e.g., "Requires M5 Pro chip")
    dependency_note: Optional[str] = None 

    # Relationship back to the laptop
    laptop: Optional["Laptop"] = Relationship(back_populates="customizations") 

# Base model for laptops, not tied to any specific database table
class LaptopBase(SQLModel):
    # Part 1: Core Identifiers & Categorization
    brand_id: uuid.UUID = Field(foreign_key="laptop_brands.id")
    model_code: str = Field(unique=True, index=True)
    product_name: str
    release_year: Optional[int] = None
    price_rm: float

    # Part 2: Processor & AI Engine
    processor_brand: Optional[str] = None
    processor_model: str 
    processor_ghz: Optional[str] = None 
    cpu_cores: Optional[int] = None
    cpu_threads: Optional[int] = None
    npu_model: Optional[str] = None
    npu_tops: Optional[float] = None
    ai_ready: bool = Field(default=False)
    ai_features: List[str] = Field(default_factory=list, sa_column=Column(JSONB))

    # Part 3: Graphics & Hardware Acceleration
    gpu_brand: Optional[str] = None
    gpu_model: str
    gpu_cores: Optional[int] = None
    media_engine_details: Optional[str] = None

    # Part 4: Memory & Storage
    ram_gb: int
    ram_type: Optional[str] = None
    ram_upgradable: bool = Field(default=False)
    max_ram_gb: Optional[int] = None

    ssd_gb: int
    storage_type: Optional[str] = None
    storage_upgradable: bool = Field(default=False)
    expansion_slots_summary: Optional[str] = None 

    # Part 5: Display & External Video
    display_size_inch: float
    display_resolution: Optional[str] = None
    display_type: Optional[str] = None
    display_refresh_rate_hz: Optional[int] = None
    display_brightness_nits: Optional[int] = None
    touchscreen: bool = Field(default=False)
    external_display_support: Optional[str] = None

    # Part 6: Build, Battery & Connectivity
    weight_kg: float
    dimensions_cm: Optional[str] = None
    battery_wh: float 
    power_supply_details: Optional[str] = None 
    os: Optional[str] = None
    colors: List[str] = Field(default_factory=list, sa_column=Column(JSONB)) 
    ports_summary: List[str] = Field(default_factory=list, sa_column=Column(JSONB)) 
    wifi_standard: Optional[str] = None 
    bluetooth_version: Optional[str] = None 

    # Part 7: Peripherals, Input & Audio
    keyboard_touchpad_details: Optional[str] = None
    audio_details: Optional[str] = None
    camera_details: Optional[str] = None
    facial_recognition: bool = Field(default=False)
    fingerprint_reader: bool = Field(default=False)

    # Part 8: Security, Certifications & Extras
    security_features: Optional[str] = None  # Captures TPM, BIOS protection, McAfee
    materials_and_certifications: Optional[str] = None  # Captures MIL-STD 810H, REACH
    microsoft_office_included: bool = Field(default=False)
    bundled_accessories: Optional[str] = None 
    warranty_details: Optional[str] = None 

    # Part 9: RAG & LLM Embedding Block
    # Used strictly for footnotes, extreme edge cases, and legal disclaimers
    raw_specs: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    image_urls: List[str] = Field(default_factory=list, sa_column=Column(JSONB))

# db tbl model for laptops, inherits from both DeclarativeBase and LaptopBase
class Laptop(LaptopBase, table=True):
    __tablename__ = "laptops"  # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    customizations: List["LaptopCustomization"] = Relationship(back_populates="laptop")
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
    __tablename__ = "laptop_embeddings"  # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", unique=True)
    embedding: Any = Field(sa_column=Column(Vector(768)))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LaptopUpdate(SQLModel):
    brand_id: Optional[uuid.UUID] = None
    model_code: Optional[str] = None
    product_name: Optional[str] = None
    price_rm: Optional[float] = None
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


class RawScrapLaptop(SQLModel, table=True):
    __tablename__ = "raw_scrap_laptops"  # type: ignore

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_url: str = Field(unique=True, index=True)
    brand_id: uuid.UUID = Field(foreign_key="laptop_brands.id")
    raw_product_name: str
    raw_prices: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    image_urls: List[str] = Field(default_factory=list, sa_column=Column(JSONB))

    raw_specs_dump: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB)
    )

    # AI Queue Tracker
    processing_status: str = Field(
        default="pending"
    )  # States: 'pending', 'processing', 'completed', 'failed'

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
