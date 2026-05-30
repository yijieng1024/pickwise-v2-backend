from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS = settings.email_verification_token_expire_hours

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
    expire = datetime.now(timezone.utc) + timedelta(hours=EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
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