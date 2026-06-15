from sqlmodel import SQLModel, Relationship, Field
from typing import Optional, List
import uuid
from app.laptops.laptop_models import Laptop

class CustomizationBase(SQLModel):
    category: str 
    option_name: str 
    price_add_rm: float 
    dependency_note: Optional[str] = None 

class LaptopCustomization(CustomizationBase, table=True):
    __tablename__ = "laptop_customizations" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    laptop_id: uuid.UUID = Field(foreign_key="laptops.id", index=True)
    laptop: Optional["Laptop"] = Relationship(back_populates="customizations")

# Schema for reading out the data
class CustomizationRead(SQLModel):
    id: uuid.UUID
    laptop_id: uuid.UUID
    category: str
    option_name: str
    price_add_rm: float
    dependency_note: Optional[str] = None

# Schema for creating data (Supports assigning to multiple laptops at once!)
class CustomizationBulkCreate(SQLModel):
    laptop_ids: List[uuid.UUID]  # Send 1 or many laptop IDs here
    category: str
    option_name: str
    price_add_rm: float
    dependency_note: Optional[str] = None

# Schema for updating a specific row
class CustomizationUpdate(SQLModel):
    category: Optional[str] = None
    option_name: Optional[str] = None
    price_add_rm: Optional[float] = None
    dependency_note: Optional[str] = None