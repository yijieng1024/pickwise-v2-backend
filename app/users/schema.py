from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Dict

class UserPreferences(BaseModel):
    budget: Optional[int] = Field(default=None, description="Maximum budget in RM")
    purpose: Optional[List[str]] = Field(default_factory=list, description="e.g., ['Office', 'Gaming', 'Programming']")
    priorities: Optional[Dict[str, int]] = Field(default_factory=dict, description="Weighting from 1-10")
    screen_size: Optional[List[str]] = Field(default_factory=list, description="e.g., ['13-14', '15-16']")
    portability: Optional[str] = Field(default=None, description="e.g., 'Ultra-light', 'Doesn't matter'")
    brand_preferences: Optional[List[str]] = Field(default_factory=list, description="e.g., ['Apple', 'Asus', 'Lenovo']")

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, description="Must be at least 8 characters long")