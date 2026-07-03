from typing import List, Optional
import uuid
from sqlmodel import SQLModel

from app.laptops.customization_model import CustomizationBase

class CustomizationRead(CustomizationBase):
    """Schema for returning customization data to the frontend"""
    id: uuid.UUID
    laptop_id: uuid.UUID

class CustomizationCreate(CustomizationBase):
    """Schema for creating a single customization for a single laptop"""
    laptop_id: uuid.UUID

class CustomizationBulkCreate(CustomizationBase):
    """Schema for assigning the exact same customization to multiple laptops at once"""
    laptop_ids: List[uuid.UUID]

class CustomizationBulkCreateByPattern(CustomizationBase):
    """Schema for assigning customization to laptops matching a model_code pattern"""
    target_pattern: str

class CustomizationUpdate(SQLModel):
    """Schema for updating an existing customization (all fields optional)"""
    category_id: Optional[uuid.UUID] = None
    option_name: Optional[str] = None
    price_add_rm: Optional[float] = None
    dependency_note: Optional[str] = None