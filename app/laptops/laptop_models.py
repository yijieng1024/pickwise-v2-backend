import uuid
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import field_validator
from sqlmodel import Relationship, SQLModel, Field
from sqlalchemy import Column, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from app.laptops.laptop_category_model import LaptopCategory

if TYPE_CHECKING:
    from app.laptops.customization_model import LaptopCustomization
    from app.taxonomy.category_model import Category


class LaptopStatus(str, Enum):
    """Catalog listing state. Only ACTIVE laptops are recommendable — see
    app/rag/retrieval.py, which filters on this for every agent search."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"  # same spelling as users.status, deliberately


LAPTOP_STATUS_VALUES = {s.value for s in LaptopStatus}


def _validate_status(value: Optional[str]) -> Optional[str]:
    """Kept as a plain VARCHAR column (like users.status) rather than a native
    Postgres enum, so adding a state later needs no ALTER TYPE migration —
    validation lives here at the API boundary instead."""
    if value is None:
        return value
    if value not in LAPTOP_STATUS_VALUES:
        raise ValueError(
            f"status must be one of {sorted(LAPTOP_STATUS_VALUES)}, got {value!r}"
        )
    return value

# Base model for laptops, not tied to any specific database table
class LaptopBase(SQLModel):
    # Part 1: Core Identifiers & Categorization
    brand_id: uuid.UUID = Field(foreign_key="laptop_brands.id")
    # The coarse product line this configuration belongs to (see
    # app/laptops/family_model.py). Nullable on purpose and null by default:
    # a null passes through family deduplication untouched, while a WRONG
    # family_id silently hides a machine the user could have bought. Nothing
    # guesses — POST /families/regroup surfaces the nulls as a work queue.
    family_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="laptop_family.id", index=True, nullable=True
    )
    model_code: str = Field(unique=True, index=True)
    product_name: str
    release_year: Optional[int] = None
    price_rm: float
    status: str = Field(
        default=LaptopStatus.ACTIVE.value,
        description="Listing status: 'active', 'inactive', or 'suspended'. Only 'active' laptops are returned by the agent's search_laptops tool.",
    )

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

    # Runs on LaptopCreate/LaptopRead (SQLModel skips validation on table=True
    # models), which is enough — every write comes in through LaptopCreate or
    # LaptopUpdate.
    @field_validator("status")
    @classmethod
    def check_status(cls, value: str) -> str:
        return _validate_status(value)  # type: ignore[return-value]

# DB Table Model
class Laptop(LaptopBase, table=True):
    __tablename__ = "laptops"  # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # 1. Customizations cascade
    customizations: List["LaptopCustomization"] = Relationship(
        back_populates="laptop",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    # 2. Embeddings cascade (1-to-1)
    embedding: Optional["LaptopEmbedding"] = Relationship(
        back_populates="laptop",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    # 3. Price History cascade
    price_history: List["LaptopPriceHistory"] = Relationship(
        back_populates="laptop",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    # 4. PickScore cache rows cascade
    pick_scores: List["LaptopPickScore"] = Relationship(
        back_populates="laptop",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

    # 5. Many-to-Many Categories link table cascade.
    # No passive_deletes: that would hand the junction rows to a database-level
    # ON DELETE CASCADE, and laptop_categories.laptop_id (like every FK to
    # laptops.id) is NO ACTION — so nothing would delete them and the parent
    # delete would fail on the FK.
    category_links: List["LaptopCategory"] = Relationship(
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "overlaps": "categories",
        }
    )
    categories: List["Category"] = Relationship(
        back_populates="laptops",
        link_model=LaptopCategory
    )


class LaptopRead(LaptopBase):
    id: uuid.UUID
    created_at: datetime

class LaptopCreate(LaptopBase):
    pass


# AI Vector Embedding
class LaptopEmbedding(SQLModel, table=True):
    __tablename__ = "laptop_embeddings"  # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", unique=True)
    laptop: Optional["Laptop"] = Relationship(back_populates="embedding")
    embedding: Any = Field(sa_column=Column(Vector(768)))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HybridSearchRequest(SQLModel):
    query: str
    budget_max: Optional[float] = None
    brand: Optional[str] = None
    top_k: int = Field(default=10, le=50)


class LaptopSearchResult(SQLModel):
    laptop_id: uuid.UUID
    product_name: str
    price_rm: float
    similarity_score: float


class LaptopPriceHistory(SQLModel, table=True):
    __tablename__ = "laptop_price_history"  # type: ignore
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    laptop: Optional["Laptop"] = Relationship(back_populates="price_history")
    price_rm: float
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LaptopPriceHistoryRead(SQLModel):
    price_rm: float
    recorded_at: datetime


# Precomputed general-mode PickScore, one row per laptop x use case. The table
# lives here, next to Laptop's other children, rather than in pickscore_general
# (which owns the weight profiles and the generation logic): Laptop.pick_scores
# names this class as a string, and SQLAlchemy can only resolve that name once
# the class has been imported. pickscore_general imports pickscore_adapter,
# which imports this module — so importing it from here would deadlock the
# import graph whenever pickscore_adapter is the entry point. It is re-exported
# from pickscore_general, so existing importers are unaffected.
class LaptopPickScore(SQLModel, table=True):
    __tablename__ = "laptop_pick_scores"  # type: ignore
    __table_args__ = (
        UniqueConstraint("laptop_id", "use_case", name="uq_laptop_pick_scores_laptop_use_case"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    laptop: Optional["Laptop"] = Relationship(back_populates="pick_scores")
    use_case: str = Field(index=True)  # slug from pickscore_general.USE_CASE_PRIORITIES
    score: int
    breakdown: list = Field(default_factory=list, sa_column=Column(JSON))
    flags: dict = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LaptopUpdate(SQLModel):
    # Part 1: Core Identifiers & Categorization
    brand_id: Optional[uuid.UUID] = None
    model_code: Optional[str] = None
    product_name: Optional[str] = None
    release_year: Optional[int] = None
    price_rm: Optional[float] = None
    status: Optional[str] = Field(
        default=None, description="'active', 'inactive', or 'suspended'"
    )

    # Part 2: Processor & AI Engine
    processor_brand: Optional[str] = None
    processor_model: Optional[str] = None
    processor_ghz: Optional[str] = None
    cpu_cores: Optional[int] = None
    cpu_threads: Optional[int] = None
    npu_model: Optional[str] = None
    npu_tops: Optional[float] = None
    ai_ready: Optional[bool] = None
    ai_features: Optional[List[str]] = None

    # Part 3: Graphics & Hardware Acceleration
    gpu_brand: Optional[str] = None
    gpu_model: Optional[str] = None
    gpu_cores: Optional[int] = None
    media_engine_details: Optional[str] = None

    # Part 4: Memory & Storage
    ram_gb: Optional[int] = None
    ram_type: Optional[str] = None
    ram_upgradable: Optional[bool] = None
    max_ram_gb: Optional[int] = None
    ssd_gb: Optional[int] = None
    storage_type: Optional[str] = None
    storage_upgradable: Optional[bool] = None
    expansion_slots_summary: Optional[str] = None

    # Part 5: Display & External Video
    display_size_inch: Optional[float] = None
    display_resolution: Optional[str] = None
    display_type: Optional[str] = None
    display_refresh_rate_hz: Optional[int] = None
    display_brightness_nits: Optional[int] = None
    touchscreen: Optional[bool] = None
    external_display_support: Optional[str] = None

    # Part 6: Build, Battery & Connectivity
    weight_kg: Optional[float] = None
    dimensions_cm: Optional[str] = None
    battery_wh: Optional[float] = None
    power_supply_details: Optional[str] = None
    os: Optional[str] = None
    colors: Optional[List[str]] = None
    ports_summary: Optional[List[str]] = None
    wifi_standard: Optional[str] = None
    bluetooth_version: Optional[str] = None

    # Part 7: Peripherals, Input & Audio
    keyboard_touchpad_details: Optional[str] = None
    audio_details: Optional[str] = None
    camera_details: Optional[str] = None
    facial_recognition: Optional[bool] = None
    fingerprint_reader: Optional[bool] = None

    # Part 8: Security, Certifications & Extras
    security_features: Optional[str] = None
    materials_and_certifications: Optional[str] = None
    microsoft_office_included: Optional[bool] = None
    bundled_accessories: Optional[str] = None
    warranty_details: Optional[str] = None

    # Part 9: External/Raw Assets
    raw_specs: Optional[Dict[str, Any]] = None
    image_urls: Optional[List[str]] = None

    @field_validator("status")
    @classmethod
    def check_status(cls, value: Optional[str]) -> Optional[str]:
        return _validate_status(value)


# --- Deferred imports: mapper registration, not usage ------------------------
# Laptop's relationships name their targets as strings ("LaptopCustomization",
# "Category"), and SQLAlchemy resolves a string only against classes that have
# actually been imported. Both live in other modules, so importing this module
# on its own — a standalone script, a test, the eval harness — used to configure
# mappers against a registry that had never heard of them and die with
#   InvalidRequestError: expression 'LaptopCustomization' failed to locate a name
# Importing any name from a module registers all of its tables, so one import
# each is enough. They sit at the bottom, after Laptop is defined, because both
# modules refer back to Laptop (under TYPE_CHECKING) — the same deferred-import
# trick that has always resolved the laptop_models <-> customization_model cycle.
# LaptopPickScore needs no entry here: it is declared in this module.
from app.laptops.customization_model import LaptopCustomization  # noqa: E402,F401
from app.taxonomy.category_model import Category  # noqa: E402,F401
# laptop_family is not a relationship target — laptops.family_id is a plain FK
# column and members are read with an explicit join — but the ForeignKey still
# names a table, and alembic/create_all can only resolve that name against a
# table already registered in SQLModel.metadata. Importing the module here
# guarantees it is, whichever of these modules a script enters through.
from app.laptops.family_model import LaptopFamily  # noqa: E402,F401
