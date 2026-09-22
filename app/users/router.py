import hmac
import re
import requests
from datetime import timedelta, datetime, timezone
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, UploadFile, File, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from sqlmodel import Session, select

from app.common.http_rate_limit import check_quota, client_ip, rate_limit
from app.database import get_session
from app.config import settings
from app.users.models import User, UserRead, Token, LaptopUserPreference
from app.users.avatar_model import UserAvatar
from app.users.auth import (
    create_password_reset_token,
    get_password_hash,
    password_reset_fingerprint,
    verify_password,
    create_access_token,
    create_email_verification_token,
    verify_email_token,
    verify_password_reset_token
)

from app.users.email import (
    send_google_account_notice_email,
    send_password_reset_email,
    send_verification_email,
)
from app.users.schema import ForgotPasswordRequest, GoogleLoginRequest, ResendVerificationRequest, ResetPasswordRequest, UserPreferences, UserRegisterRequest, UserProfile
from app.users.auth import get_current_user
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

# One response for every outcome of an endpoint that takes an email address:
# sent, already verified, no such account, Google account. Any variation is an
# oracle for "is this address registered here".
GENERIC_EMAIL_RESPONSE = {
    "message": "If that email is registered, we've sent it a link. Please check your inbox."
}

# Both a bad/expired token and a token whose fingerprint no longer matches
# come back as this. "User not found" used to be a 404 here, which told an
# attacker that a guessed address was NOT registered.
INVALID_RESET_MESSAGE = "Invalid or expired password reset link."

# Sending endpoints are capped per address as well as per IP: the per-IP limit
# alone does not protect the Brevo quota (300 emails/day) against a
# distributed caller, and the daily cap is what bounds the worst case.
_EMAIL_COOLDOWN_SECONDS = 60
_EMAIL_DAILY_LIMIT = 5
_EMAIL_DAY_SECONDS = 86_400


# ===========================================================================
# TEMPORARY DIAGNOSTIC — REMOVE AFTER THE X-FORWARDED-FOR SHAPE IS CONFIRMED
# ===========================================================================
# Why it exists: `client_ip()` currently takes the FIRST X-Forwarded-For
# entry, which is whatever the CLIENT sent. A caller can therefore forge a
# fresh IP per request and walk straight past every per-IP limit, including
# the ones protecting the Brevo 300/day quota. The fix is to count from the
# RIGHT — the rightmost entries are appended by infrastructure we trust —
# but the correct offset depends on how many proxies Render puts in front of
# this app, and that is an empirical question, not a guessable one.
#
# So: deploy, sign in a few times, read the log, THEN change the logic.
# Logged at WARNING so it survives any sane production log level and greps
# cleanly. It prints IP addresses, so it must not outlive the measurement.
def _log_forwarding_headers(request: Request, endpoint: str) -> None:
    forwarded = request.headers.get("x-forwarded-for")
    entries = [p.strip() for p in forwarded.split(",")] if forwarded else []
    logger.warning(
        "XFF-PROBE endpoint=%s socket=%s x-forwarded-for=%r entries=%d parsed=%r "
        "x-real-ip=%r forwarded=%r current_client_ip=%s",
        endpoint,
        request.client.host if request.client else None,
        forwarded,
        len(entries),
        entries,
        request.headers.get("x-real-ip"),
        request.headers.get("forwarded"),
        client_ip(request),
    )


# ===========================================================================


def _may_send_to(bucket: str, email: str) -> bool:
    """Per-address cooldown + daily cap. Silent: the caller answers the same
    either way, or the throttle itself becomes the enumeration oracle."""
    if not check_quota(f"{bucket}:cooldown", email, 1, _EMAIL_COOLDOWN_SECONDS):
        return False
    return check_quota(f"{bucket}:daily", email, _EMAIL_DAILY_LIMIT, _EMAIL_DAY_SECONDS)

def _ensure_account_active(user: User) -> None:
    """Shared status gate for every login path (password + Google)."""
    if user.status == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended.",
        )
    if user.status == "inactive":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is inactive.",
        )

@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("register", 5, 3600))],
)
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
    invalid = HTTPException(status_code=400, detail="Invalid or expired verification token")
    if not email:
        raise invalid

    # find user by email. A missing account is reported as an invalid token,
    # not a 404 — the address inside a signed token is not a secret, but
    # answering differently here is still a free membership check.
    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        raise invalid

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

@router.post(
    "/login",
    response_model=Token,
    dependencies=[Depends(rate_limit("login", 10, 300))],
)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
):
    _log_forwarding_headers(request, "login")

    bad_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username/email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Per-account limit, on top of the per-IP one above. The IP limit is the
    # weaker of the two here: an attacker spreading a credential-stuffing run
    # across many addresses controls their own IP but not which account they
    # are attacking. Deliberately the same 401 as a wrong password — a 429
    # would confirm the account exists and is worth queuing for later.
    if not check_quota("login:account", form_data.username, 10, 300):
        raise bad_credentials

    # search user by username or email
    statement = select(User).where(
        (User.username == form_data.username) | (User.email == form_data.username)
    )
    user = session.exec(statement).first()

    # verify password (social-login accounts have no local password)
    if not user or not user.password or not verify_password(form_data.password, user.password):
        raise bad_credentials

    _ensure_account_active(user)

    # check if email is verified
    if not user.is_verified:
        # Structured detail so the frontend can branch on `code` and offer the
        # resend button, instead of pattern-matching English prose that any
        # copy edit would silently break.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "email_unverified",
                "message": "Please verify your email before logging in.",
            },
        )

    # generate JWT token
    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}


def _issue_access_token(user: User) -> dict:
    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )
    return {"access_token": access_token, "token_type": "bearer"}


def _generate_unique_username(session: Session, email: str) -> str:
    """Derive a unique username from the email prefix (no '@' allowed in usernames)."""
    base = re.sub(r"[^a-zA-Z0-9._-]", "", email.split("@")[0]) or "user"
    candidate = base
    suffix = 0
    while session.exec(select(User).where(User.username == candidate)).first():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def _import_google_avatar(session: Session, user: User, picture_url: Optional[str]) -> None:
    """Best-effort copy of the Google profile photo into user_avatars, so the
    frontend serves every avatar from the same gateway URL. Only runs when the
    user has no avatar yet (never overwrites an uploaded or deleted one), and
    never fails the login."""
    if not picture_url:
        return
    try:
        if session.exec(select(UserAvatar).where(UserAvatar.user_id == user.id)).first():
            return
        # Google serves 96px by default; ask for 256px for retina displays
        resp = requests.get(picture_url.replace("=s96-c", "=s256-c"), timeout=5)
        resp.raise_for_status()
        data = resp.content
        if len(data) > MAX_AVATAR_BYTES:
            return
        content_type = _sniff_image_type(data)
        if not content_type:
            return
        session.add(UserAvatar(user_id=user.id, content_type=content_type, data=data))
        session.commit()
    except Exception:
        session.rollback()  # avatar import is cosmetic — never block login


@router.post(
    "/google",
    response_model=Token,
    # Every call verifies the ID token against Google's endpoint, so this is
    # an outbound request we pay for on someone else's schedule.
    dependencies=[Depends(rate_limit("google", 20, 300))],
)
def google_login(request: GoogleLoginRequest, session: Session = Depends(get_session)):
    """
    Sign in with Google. The frontend obtains an ID token via Google Identity
    Services and posts it here; we verify it against our OAuth client ID and
    find-or-create the user, linking by Google 'sub' first, then by email.
    """
    if not settings.google_oauth_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google login is not configured on this server."
        )

    try:
        # verifies signature, expiry, issuer, and audience (our client ID)
        claims = google_id_token.verify_oauth2_token(
            request.id_token,
            google_requests.Request(),
            settings.google_oauth_client_id,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token"
        )

    email = claims.get("email")
    if not email or not claims.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account has no verified email"
        )
    google_sub = claims["sub"]

    # 1. Returning Google user — matched by stable Google subject ID
    user = session.exec(select(User).where(User.provider_sub == google_sub)).first()
    if user:
        _ensure_account_active(user)
        return _issue_access_token(user)

    # 2. Existing local account with the same email — link it to Google.
    #    Google has verified the email, so the account counts as verified too.
    user = session.exec(select(User).where(User.email == email)).first()
    if user:
        _ensure_account_active(user)
        user.provider_sub = google_sub
        user.is_verified = True
        session.add(user)
        session.commit()
        session.refresh(user)
        _import_google_avatar(session, user, claims.get("picture"))
        return _issue_access_token(user)

    # 3. First visit — create a new account (no local password)
    user = User(
        username=_generate_unique_username(session, email),
        email=email,
        password=None,
        auth_provider="google",
        provider_sub=google_sub,
        is_verified=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    _import_google_avatar(session, user, claims.get("picture"))
    return _issue_access_token(user)


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

# --- Avatar gateway -------------------------------------------------------
# Bytes live in the user_avatars table (Render's filesystem is ephemeral, so
# disk storage would be wiped on every deploy). Serve URL is public so the
# frontend can point <img src> straight at it.

MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB

# magic-byte signatures — don't trust the client's Content-Type header alone
_IMAGE_SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]


def _sniff_image_type(data: bytes) -> str | None:
    for magic, content_type in _IMAGE_SIGNATURES:
        if data.startswith(magic):
            return content_type
    # WEBP: RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.put("/me/avatar")
async def upload_my_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Upload or replace the current user's avatar (JPEG/PNG/WebP, max 2 MB)."""
    data = await file.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Avatar must be 2 MB or smaller",
        )

    content_type = _sniff_image_type(data)
    if not content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Avatar must be a JPEG, PNG, or WebP image",
        )

    avatar = session.exec(
        select(UserAvatar).where(UserAvatar.user_id == current_user.id)
    ).first()

    if avatar:
        avatar.data = data
        avatar.content_type = content_type
        avatar.updated_at = datetime.now(timezone.utc)
    else:
        avatar = UserAvatar(user_id=current_user.id, content_type=content_type, data=data)

    session.add(avatar)
    session.commit()

    return {
        "message": "Avatar updated successfully",
        "avatar_url": f"/api/v2/auth/avatar/{current_user.id}",
        "content_type": content_type,
        "size_bytes": len(data),
    }


@router.get("/avatar/{user_id}")
def get_avatar(user_id: UUID, session: Session = Depends(get_session)):
    """Public — serve a user's avatar image bytes."""
    avatar = session.exec(
        select(UserAvatar).where(UserAvatar.user_id == user_id)
    ).first()

    if not avatar:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Avatar not found")

    return Response(
        content=avatar.data,
        media_type=avatar.content_type,
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.delete("/me/avatar", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_avatar(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Remove the current user's avatar."""
    avatar = session.exec(
        select(UserAvatar).where(UserAvatar.user_id == current_user.id)
    ).first()

    if not avatar:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Avatar not found")

    session.delete(avatar)
    session.commit()
    return None


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("forgot_password", 10, 3600))],
)
def forgot_password(
    request: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.email == request.email)).first()

    if user and _may_send_to("forgot_password", request.email):
        if user.password is None:
            # Google-only account: there is no password to reset. Gated on the
            # hash being absent rather than on auth_provider, because a Google
            # login that linked to an existing local account keeps its
            # password and must still be resettable.
            background_tasks.add_task(
                send_google_account_notice_email,
                email_to=user.email,
            )
        else:
            reset_token = create_password_reset_token(
                email=user.email, password_hash=user.password
            )
            background_tasks.add_task(
                send_password_reset_email,
                email_to=user.email,
                token=reset_token
            )

    return GENERIC_EMAIL_RESPONSE


@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("resend_verification", 10, 3600))],
)
def resend_verification(
    request: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """
    Re-send the verification link. Login is blocked until the address is
    confirmed, so without this a user whose first email was lost, expired or
    never sent (which is every registration made while SMTP was blocked on
    Render) has no way back into their own account.

    Answers identically whether the address is unknown, already verified, a
    Google account, or genuinely re-sent.
    """
    user = session.exec(select(User).where(User.email == request.email)).first()

    if (
        user
        and not user.is_verified
        and user.password is not None  # Google accounts are verified on create
        and _may_send_to("resend_verification", request.email)
    ):
        background_tasks.add_task(
            send_verification_email,
            user.email,
            create_email_verification_token(user.email),
        )

    return GENERIC_EMAIL_RESPONSE


@router.post(
    "/reset-password",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit("reset_password", 10, 3600))],
)
def reset_password(
    request: ResetPasswordRequest,
    session: Session = Depends(get_session)
):
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=INVALID_RESET_MESSAGE,
    )

    decoded = verify_password_reset_token(request.token)
    if not decoded:
        raise invalid
    email, fingerprint = decoded

    user = session.exec(select(User).where(User.email == email)).first()
    # Same error as a bad token, not a 404: the old 404 "User not found" let
    # anyone test whether an address was registered.
    if not user:
        raise invalid

    # Single-use enforcement. The fingerprint is bound to the password hash
    # the token was minted against, so the first successful reset (or any
    # other password change) invalidates every outstanding link.
    if not hmac.compare_digest(fingerprint, password_reset_fingerprint(user.password)):
        raise invalid

    user.password = get_password_hash(request.new_password)
    session.add(user)
    session.commit()

    return {"message": "Password has been successfully reset."}