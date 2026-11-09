import re
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from typing import List, Literal, Optional, Dict

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
    """PUT /me/profile body. Empty since birthday, gender and occupation were
    retired (2026-11-09; the frontend stopped sending them in 48a2357). Extra
    fields are forbidden, so sending one is a 422 rather than silently ignored."""
    model_config = ConfigDict(extra="forbid")

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

class GoogleLoginRequest(BaseModel):
    id_token: str = Field(description="Google ID token (JWT) obtained by the frontend via Google Identity Services")

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResendVerificationRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, description="Must be at least 8 characters long")

class UserRoleUpdate(BaseModel):
    role: Literal["user", "admin"]

class UserStatusUpdate(BaseModel):
    status: Literal["active", "inactive", "suspended"]


class UserPreferencesRead(UserPreferences):
    """Response shape: the answers plus when they were last saved. Separate from the
    request body so a client cannot set updated_at. The wizard compares it with its
    browser draft and keeps whichever is newer; null on accounts with no row."""
    updated_at: Optional[datetime] = None
