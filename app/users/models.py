from typing import Any, Dict, Optional
import uuid
from datetime import datetime, timezone
from sqlmodel import Relationship, JSON, Column, SQLModel, Field

class User(SQLModel, table=True):
    __tablename__ = "users" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    password: str
    role: str = Field(default="user")
    is_verified: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Relationship to preferences
    preferences: Optional["LaptopUserPreference"] = Relationship(back_populates="user")

class LaptopUserPreference(SQLModel, table=True):
    __tablename__ = "laptop_user_preference" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    budget: Optional[int] = Field(default=None, description="Maximum budget in RM")
    purpose: Optional[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    priorities: Optional[Dict[str, int]] = Field(default_factory=dict, sa_column=Column(JSON))
    screen_size: Optional[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
    portability: Optional[str] = Field(default=None)
    brand_preferences: Optional[list[str]] = Field(default_factory=list, sa_column=Column(JSON))
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
    created_at: datetime

# token response model for authentication
class Token(SQLModel):
    access_token: str
    token_type: str