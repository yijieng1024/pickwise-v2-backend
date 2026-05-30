import uuid
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field

class User(SQLModel, table=True):
    __tablename__ = "users" # type: ignore
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    password: str
    role: str = Field(default="user")
    is_verified: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# frontend receive data for creating a new user
class UserCreate(SQLModel):
    username: str
    email: str
    password: str

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