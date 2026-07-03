from typing import Any, Dict, Optional
import uuid
from datetime import datetime, timezone, date
from enum import Enum
from sqlmodel import Relationship, JSON, Column, SQLModel, Field

class GenderEnum(str, Enum):
    """Gender enum for user profile"""
    MALE = "Male"
    FEMALE = "Female"
    OTHER = "Other"

class User(SQLModel, table=True):
    __tablename__ = "users" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    password: str
    role: str = Field(default="user")
    is_verified: bool = Field(default=False)
    birthday: Optional[date] = Field(default=None, description="User's date of birth")
    gender: Optional[str] = Field(default=None, description="User's gender (Male, Female, Other)")
    occupation: Optional[str] = Field(default=None, description="User's occupation")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Relationship to preferences
    preferences: Optional["LaptopUserPreference"] = Relationship(back_populates="user")

class LaptopUserPreference(SQLModel, table=True):
    __tablename__ = "laptop_user_preference" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    budget: Optional[Dict[str, Optional[float]]] = Field(
        default=None, sa_column=Column(JSON), description="{min, max} RM range; max=null means no upper limit"
    )
    purpose: Optional[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    priorities: Optional[Dict[str, int]] = Field(default_factory=dict, sa_column=Column(JSON))
    screen_size: Optional[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    portability: Optional[str] = Field(default=None)
    brand_preferences: Optional[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    tech_savviness: Optional[str] = Field(default=None, description="Tech-savviness level: 'Very tech-savvy', 'Somewhat tech-savvy', or 'Not very tech-savvy'")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Relationship back to user
    user: Optional[User] = Relationship(back_populates="preferences")

# data for read user info (password is optional)
class UserRead(SQLModel):
    id: uuid.UUID
    username: str
    email: str
    role: str
    is_verified: bool
    birthday: Optional[date]
    gender: Optional[str]
    occupation: Optional[str]
    created_at: datetime

# token response model for authentication
class Token(SQLModel):
    access_token: str
    token_type: str