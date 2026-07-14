from datetime import timedelta, datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from app.database import get_session
from app.config import settings
from app.users.models import User, UserRead, Token, LaptopUserPreference
from app.users.auth import (
    create_password_reset_token,
    get_password_hash,
    verify_password,
    create_access_token,
    create_email_verification_token,
    verify_email_token,
    verify_password_reset_token
)

from app.users.email import send_password_reset_email, send_verification_email
from app.users.schema import ForgotPasswordRequest, ResetPasswordRequest, UserPreferences, UserRegisterRequest, UserProfile
from app.users.auth import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(
    request: UserRegisterRequest,
    background_tasks: BackgroundTasks, 
    session: Session = Depends(get_session)
):
     
    statement = select(User).where(
        (User.username == request.username) | (User.email == request.email)
    )
    if session.exec(statement).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
    
    db_user = User(
        username=request.username,
        email=request.email,
        password=get_password_hash(request.password)
    )
    
    session.add(db_user)
    session.commit()
    session.refresh(db_user)

    verification_token = create_email_verification_token(db_user.email)
    background_tasks.add_task(send_verification_email, db_user.email, verification_token)
    
    return {"message": "User created successfully. Please check your email to verify."}


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

@router.get("/me/profile", response_model=UserRead)
def get_my_profile(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    """Get current user's profile information"""
    user = session.exec(select(User).where(User.id == current_user.id)).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return user

@router.put("/me/profile", response_model=UserRead)
def update_my_profile(
    profile_in: UserProfile,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    """Update current user's profile information"""
    user = session.exec(select(User).where(User.id == current_user.id)).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Update only provided fields
    profile_data = profile_in.model_dump(exclude_unset=True)
    for key, value in profile_data.items():
        setattr(user, key, value)
    
    session.add(user)
    session.commit()
    session.refresh(user)
    
    return user

@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    # search user by username or email
    statement = select(User).where(
        (User.username == form_data.username) | (User.email == form_data.username)
    )
    user = session.exec(statement).first()

    # verify password
    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username/email or password",
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
        data={"sub": str(user.id)},
        expires_delta=access_token_expires
    )
    
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/me/preferences", response_model=UserPreferences)
def get_my_preferences(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    # Get user with preferences relationship loaded
    user = session.exec(select(User).where(User.id == current_user.id)).first()
    
    if not user or not user.preferences:
        return UserPreferences()
    
    # Convert database model to schema
    prefs = user.preferences
    return UserPreferences(
        budget=prefs.budget,
        purpose=prefs.purpose or [],
        priorities=prefs.priorities or {},
        screen_size=prefs.screen_size or [],
        portability=prefs.portability,
        brand_preferences=prefs.brand_preferences or [],
        tech_savviness=prefs.tech_savviness
    )

@router.put("/me/preferences", response_model=UserPreferences)
def update_my_preferences(
    preferences_in: UserPreferences,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user)
):
    # Get user with preferences relationship
    user = session.exec(select(User).where(User.id == current_user.id)).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if user already has preferences record
    existing_pref = session.exec(
        select(LaptopUserPreference).where(LaptopUserPreference.user_id == user.id)
    ).first()
    
    if existing_pref:
        # Update existing preferences
        pref_data = preferences_in.model_dump(exclude_unset=True)
        for key, value in pref_data.items():
            setattr(existing_pref, key, value)
        existing_pref.updated_at = datetime.now(timezone.utc)
        session.add(existing_pref)
    else:
        # Create new preferences record
        new_pref = LaptopUserPreference(
            user_id=user.id,
            budget=preferences_in.budget,
            purpose=preferences_in.purpose,
            priorities=preferences_in.priorities,
            screen_size=preferences_in.screen_size,
            portability=preferences_in.portability,
            brand_preferences=preferences_in.brand_preferences,
            tech_savviness=preferences_in.tech_savviness
        )
        session.add(new_pref)
    
    session.commit()
    
    # Fetch updated preferences
    updated_pref = session.exec(
        select(LaptopUserPreference).where(LaptopUserPreference.user_id == user.id)
    ).first()
    
    return UserPreferences(
        budget=updated_pref.budget, #type: ignore
        purpose=updated_pref.purpose or [], #type: ignore
        priorities=updated_pref.priorities or {}, #type: ignore
        screen_size=updated_pref.screen_size or [], #type: ignore
        portability=updated_pref.portability, #type: ignore
        brand_preferences=updated_pref.brand_preferences or [], #type: ignore
        tech_savviness=updated_pref.tech_savviness #type: ignore
    )

@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
def forgot_password(
    request: ForgotPasswordRequest, 
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.email == request.email)).first()

    if user:
        reset_token = create_password_reset_token(email=user.email)

        background_tasks.add_task(
            send_password_reset_email, 
            email_to=user.email, 
            token=reset_token
        )
        
    return {"message": "If that email exists in our system, a reset link has been sent."}

@router.post("/reset-password", status_code=status.HTTP_200_OK)
def reset_password(
    request: ResetPasswordRequest,
    session: Session = Depends(get_session)
):
    
    email = verify_password_reset_token(request.token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset token."
        )

    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )

    user.password = get_password_hash(request.new_password)
    session.add(user)
    session.commit()
    
    return {"message": "Password has been successfully reset."}