from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session

from app.config import settings
from app.database import get_session, session_scope
from app.users.models import User
from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# send credentials to get a token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v2/auth/login")

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
        
    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded_jwt

def create_email_verification_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.email_verification_token_expire_hours)
    to_encode = {"sub": email, "exp": expire, "scope": "email_verification"}
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)

def verify_email_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        if payload.get("scope") != "email_verification":
            return None
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def _resolve_user(token: str, session: Session) -> User:
    """Decode *token* and load the account it names, or raise 401/403."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])

        user_identifier: str = payload.get("sub")  # type: ignore

        if user_identifier is None:
            raise credentials_exception

    except jwt.InvalidTokenError:
        # Catch expired or fake tokens
        raise credentials_exception

    user = session.get(User, user_identifier)

    if user is None:
        raise credentials_exception

    # A token can outlive an admin action taken against the account (7-day
    # expiry) — re-check status on every request, not just at login.
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not active.",
        )

    return user


def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session)
) -> User:
    return _resolve_user(token, session)


def get_current_user_detached(token: str = Depends(oauth2_scheme)) -> User:
    """
    `get_current_user` for endpoints that must not hold a pooled connection.

    FastAPI caches dependencies per request, so an endpoint that drops
    `Depends(get_session)` still keeps a connection checked out for the whole
    response if it authenticates through `get_current_user` — the same session
    object is shared. That is the wrong trade for a `StreamingResponse`, whose
    "response" lasts as long as the SSE stream (see app/database.py).

    The returned `User` is **detached**: its columns are readable, but it has
    no session, so relationship access lazy-loads into an error and writes to
    it are ignored unless it is merged into a live session. Depend on this only
    where the account is read, not written.
    """
    with session_scope(expire_on_commit=False) as session:
        return _resolve_user(token, session)

def create_password_reset_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode = {"sub": email, "exp": expire, "scope": "password_reset"}
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)

def verify_password_reset_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        if payload.get("scope") != "password_reset":
            return None
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    
def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Admin access required."
        )
    return current_user