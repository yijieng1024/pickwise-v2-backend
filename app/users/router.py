from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from app.database import get_session
from app.config import settings
from app.users.models import User, UserCreate, UserRead, Token
from app.users.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    create_email_verification_token,
    verify_email_token
)

from app.users.email import send_real_verification_email

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(user: UserCreate, background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    # check if username or email already exists 
    statement = select(User).where((User.username == user.username) | (User.email == user.email))
    if session.exec(statement).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
    
    # save user to database
    db_user = User(
        username=user.username,
        email=user.email,
        password=get_password_hash(user.password) 
    )
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    
    # send verification email in the background
    verification_token = create_email_verification_token(user.email)
    background_tasks.add_task(send_real_verification_email, user.email, verification_token)
    
    return db_user


@router.get("/verify-email")
def verify_email(token: str, session: Session = Depends(get_session)):
    # decode token to get email
    email = verify_email_token(token)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    
    # find user by email
    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user.is_verified:
        return {"message": "Email is already verified"}
    
    # update user to set is_verified = True
    user.is_verified = True
    session.add(user)
    session.commit()
    
    return {"message": "Email verified successfully! You can now log in."}


@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    # search user by username
    statement = select(User).where(User.username == form_data.username)
    user = session.exec(statement).first()
    
    # 2. verify password
    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    # check if email is verified
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email before logging in."
        )
    
    # generate JWT token
    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user.username}, 
        expires_delta=access_token_expires
    )
    
    return {"access_token": access_token, "token_type": "bearer"}