import re
from datetime import date
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import List, Optional, Dict

class UserRegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str = Field(min_length=8, description="Must be at least 8 characters")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Usernames must not look like email addresses — login accepts either,
        so an '@' in a username could shadow another user's email."""
        v = v.strip()
        if not v:
            raise ValueError("Username must not be empty.")
        if "@" in v:
            raise ValueError("Username must not contain '@'.")
        return v

    @field_validator('password')
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        """
        Enforces password strength:
        - At least one uppercase letter
        - At least one lowercase letter
        - At least one number
        """
        if not re.match(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)", v):
            raise ValueError(
                "Password must contain at least one uppercase letter, "
                "one lowercase letter, and one number."
            )
        return v

class UserProfile(BaseModel):
    """User profile information update"""
    birthday: Optional[date] = Field(default=None, description="User's date of birth (YYYY-MM-DD)")
    gender: Optional[str] = Field(default=None, description="User's gender: 'Male', 'Female', or 'Other'")
    occupation: Optional[str] = Field(default=None, description="User's occupation")
    
    @field_validator('gender')
    @classmethod
    def validate_gender(cls, v: Optional[str]) -> Optional[str]:
        """Validate gender is one of the allowed values"""
        if v is not None and v not in ["Male", "Female", "Other"]:
            raise ValueError("Gender must be 'Male', 'Female', or 'Other'")
        return v

class UserPreferences(BaseModel):
    budget: Optional[Dict[str, Optional[float]]] = Field(default=None, description="{min, max} RM range; max=null means no upper limit")
    purpose: Optional[List[str]] = Field(default_factory=list, description="e.g., ['Office', 'Gaming', 'Programming']")
    priorities: Optional[Dict[str, int]] = Field(default_factory=dict, description="Weighting from 1-10")
    screen_size: Optional[List[str]] = Field(default_factory=list, description="e.g., ['13-14', '15-16']")
    portability: Optional[str] = Field(default=None, description="e.g., 'Ultra-light', 'Doesn't matter'")
    brand_preferences: Optional[List[str]] = Field(default_factory=list, description="e.g., ['Apple', 'Asus', 'Lenovo']")
    tech_savviness: Optional[str] = Field(default=None, description="Tech-savviness level: 'Very tech-savvy', 'Somewhat tech-savvy', or 'Not very tech-savvy'")
    
    @field_validator('tech_savviness')
    @classmethod
    def validate_tech_savviness(cls, v: Optional[str]) -> Optional[str]:
        """Validate tech_savviness is one of the allowed values"""
        if v is not None and v not in ["Very tech-savvy", "Somewhat tech-savvy", "Not very tech-savvy"]:
            raise ValueError("Tech-savviness must be 'Very tech-savvy', 'Somewhat tech-savvy', or 'Not very tech-savvy'")
        return v

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, description="Must be at least 8 characters long")