import logging
import os
import secrets
from datetime import timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.cache import get_redis
from app.core.db.session import get_db
from app.core.email import PasswordResetEmailError, send_password_reset_email
from app.core.security import (
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
    REMEMBER_ME_EXPIRE_DAYS,
    STANDARD_SESSION_EXPIRE_HOURS,
    constant_time_compare,
    create_access_token,
    create_password_reset_token,
    hash_password,
    hash_token,
    verify_password,
    verify_token,
)
from app.models.university import University
from app.models.users import User

router = APIRouter(
    prefix="/users",
    tags=["users"],
)

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/users/login", auto_error=False)
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "want_session")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN")
APP_ENV = os.getenv("APP_ENV", "development").lower()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", APP_ENV).lower() in {
    "1",
    "true",
    "yes",
    "on",
    "production",
}
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none" if COOKIE_SECURE else "lax").lower()
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


class UserCreate(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    job_title: Optional[str] = None
    university_id: Optional[UUID] = None
    role: str = "user"


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    job_title: Optional[str] = None
    university_id: Optional[UUID] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None


class UserResponse(BaseModel):
    id: UUID
    full_name: str
    email: str
    job_title: Optional[str] = None
    is_active: bool
    role: str
    university_id: Optional[UUID] = None
    hub_id: Optional[UUID] = None

    class Config:
        from_attributes = True


class HubMemberResponse(BaseModel):
    id: UUID
    full_name: str
    email: str
    job_title: Optional[str] = None
    is_active: bool
    role: str
    university_id: Optional[UUID] = None
    university_name: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    profile_completed: bool


class MessageResponse(BaseModel):
    detail: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    remember_me: bool = False


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=20)
    new_password: str = Field(..., min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class ProfileSetupRequest(BaseModel):
    university_id: Optional[UUID] = None


class ClerkSessionRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=200)
    job_title: Optional[str] = Field(default=None, max_length=200)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def set_auth_cookie(response: Response, token: str, remember_me: bool) -> None:
    cookie_kwargs = {
        "key": SESSION_COOKIE_NAME,
        "value": token,
        "httponly": True,
        "secure": COOKIE_SECURE,
        "samesite": COOKIE_SAMESITE,
        "path": "/",
    }
    if COOKIE_DOMAIN:
        cookie_kwargs["domain"] = COOKIE_DOMAIN
    if remember_me:
        max_age = REMEMBER_ME_EXPIRE_DAYS * 24 * 60 * 60
        cookie_kwargs["max_age"] = max_age
        cookie_kwargs["expires"] = max_age
    response.set_cookie(**cookie_kwargs)


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )


def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    auth_token = token or request.cookies.get(SESSION_COOKIE_NAME)
    if not auth_token:
        raise credentials_exception

    if auth_token.lower().startswith("bearer "):
        auth_token = auth_token.split(" ", 1)[1]

    payload = verify_token(auth_token)
    if payload is None:
        raise credentials_exception

    email = payload.get("sub")
    if email is None:
        raise credentials_exception

    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if user is None:
        raise credentials_exception

    return user


@router.put("/me/complete-profile", response_model=UserResponse)
def complete_profile(
    profile_data: ProfileSetupRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.university_id = profile_data.university_id
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/clerk-session", response_model=UserResponse)
def create_clerk_session(
    payload: ClerkSessionRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    email = normalize_email(str(payload.email))
    user = db.query(User).filter(User.email == email).first()

    if user is None:
        user = User(
            full_name=payload.full_name.strip(),
            email=email,
            job_title=payload.job_title,
            role="user",
            password_hash=hash_password(secrets.token_urlsafe(32)),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        updates_made = False
        normalized_name = payload.full_name.strip()
        if normalized_name and user.full_name != normalized_name:
            user.full_name = normalized_name
            updates_made = True

        if payload.job_title and user.job_title != payload.job_title:
            user.job_title = payload.job_title
            updates_made = True

        if updates_made:
            db.commit()
            db.refresh(user)

    access_token_expires = timedelta(hours=STANDARD_SESSION_EXPIRE_HOURS)
    access_token = create_access_token(data={"sub": user.email}, expires_delta=access_token_expires)
    set_auth_cookie(response, access_token, remember_me=False)
    return user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    email = normalize_email(str(user.email))
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    db_user = User(
        full_name=user.full_name,
        email=email,
        job_title=user.job_title,
        university_id=user.university_id,
        role=user.role,
        password_hash=hash_password(user.password),
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@router.post("/login", response_model=Token)
def login(login_data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    email = normalize_email(str(login_data.email))
    user = db.query(User).filter(User.email == email).first()

    if not user or not verify_password(login_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    access_token_expires = (
        timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
        if login_data.remember_me
        else timedelta(hours=STANDARD_SESSION_EXPIRE_HOURS)
    )
    access_token = create_access_token(data={"sub": user.email}, expires_delta=access_token_expires)
    set_auth_cookie(response, access_token, login_data.remember_me)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": int(access_token_expires.total_seconds()),
        "profile_completed": user.university_id is not None,
    }


@router.post("/logout", response_model=MessageResponse)
def logout(response: Response):
    clear_auth_cookie(response)
    return {"detail": "Logged out successfully"}


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    requested_email = normalize_email(str(payload.email))
    generic_response = {
        "detail": "If an account exists for that email, a reset link has been sent."
    }

    user = db.query(User).filter(User.email == requested_email).first()
    if not user or not user.is_active:
        logger.info("Password reset requested for unavailable account: %s", requested_email)
        return generic_response

    raw_token = create_password_reset_token()
    token_hash_value = hash_token(raw_token)
    expires_in_seconds = PASSWORD_RESET_TOKEN_EXPIRE_MINUTES * 60
    reset_link = f"{FRONTEND_URL.rstrip('/')}/reset-password?token={raw_token}"

    redis_client = get_redis()
    user_key = f"password-reset:user:{user.id}"
    token_key = f"password-reset:token:{token_hash_value}"
    previous_hash = redis_client.get(user_key)
    if previous_hash:
        redis_client.delete(f"password-reset:token:{previous_hash}")

    try:
        pipeline = redis_client.pipeline()
        pipeline.setex(user_key, expires_in_seconds, token_hash_value)
        pipeline.setex(token_key, expires_in_seconds, str(user.id))
        pipeline.execute()
    except Exception as exc:
        logger.exception(
            "Failed to store password reset token for user_id=%s email=%s",
            user.id,
            requested_email,
        )
        redis_client.delete(user_key)
        redis_client.delete(token_key)
        raise HTTPException(
            status_code=503,
            detail="Password reset is temporarily unavailable. Please try again shortly.",
        ) from exc

    try:
        delivery_result = send_password_reset_email(user.email, reset_link)
        logger.info(
            "Password reset email handled for user_id=%s email=%s delivery_status=%s",
            user.id,
            requested_email,
            delivery_result.get("status", "unknown"),
        )
    except PasswordResetEmailError as exc:
        logger.exception(
            "Password reset email delivery failed for user_id=%s email=%s",
            user.id,
            requested_email,
        )
        redis_client.delete(user_key)
        redis_client.delete(token_key)
        raise HTTPException(
            status_code=503,
            detail="We couldn't deliver reset instructions right now. Please try again later.",
        ) from exc

    return {"detail": delivery_result.get("message", generic_response["detail"])}


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    redis_client = get_redis()
    provided_hash = hash_token(payload.token)
    user_id_value = redis_client.get(f"password-reset:token:{provided_hash}")

    if not user_id_value:
        raise HTTPException(
            status_code=400,
            detail="This password reset link is invalid or has expired.",
        )

    expected_hash = redis_client.get(f"password-reset:user:{user_id_value}")
    if not expected_hash or not constant_time_compare(expected_hash, provided_hash):
        raise HTTPException(
            status_code=400,
            detail="This password reset link is invalid or has expired.",
        )

    user = db.query(User).filter(User.id == UUID(user_id_value)).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=400,
            detail="This password reset link is invalid or has expired.",
        )

    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(
            status_code=400,
            detail="Please choose a password you have not used before.",
        )

    user.password_hash = hash_password(payload.new_password)
    db.commit()

    redis_client.delete(f"password-reset:token:{provided_hash}")
    redis_client.delete(f"password-reset:user:{user.id}")

    return {"detail": "Password reset successful. Please sign in with your new password."}


@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/hub-members", response_model=List[HubMemberResponse])
def read_hub_members(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.hub_id:
        raise HTTPException(
            status_code=400,
            detail="Complete your profile with an affiliated institution to view hub members.",
        )

    rows = (
        db.query(User, University)
        .join(University, User.university_id == University.id)
        .filter(University.hub_id == current_user.hub_id)
        .order_by(User.full_name.asc(), User.email.asc())
        .all()
    )

    return [
        HubMemberResponse(
            id=user.id,
            full_name=(user.full_name or "").strip() or user.email,
            email=user.email,
            job_title=user.job_title,
            is_active=bool(user.is_active),
            role=user.role,
            university_id=user.university_id,
            university_name=university.name if university else None,
        )
        for user, university in rows
    ]


@router.post("/me/change-password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Your current password is incorrect.")

    if verify_password(payload.new_password, current_user.password_hash):
        raise HTTPException(
            status_code=400,
            detail="Please choose a password you have not used before.",
        )

    current_user.password_hash = hash_password(payload.new_password)
    db.commit()

    return {"detail": "Password updated successfully."}


@router.get("/{user_id}", response_model=UserResponse)
def read_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: UUID,
    user: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.id != user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to update this user")

    db_user = db.query(User).filter(User.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = user.model_dump(exclude_unset=True)

    if "password" in update_data:
        if current_user.role != "admin":
            raise HTTPException(
                status_code=400,
                detail="Use the settings page to change your password.",
            )
        update_data["password_hash"] = hash_password(update_data.pop("password"))

    if "email" in update_data and update_data["email"]:
        update_data["email"] = normalize_email(str(update_data["email"]))

    if current_user.id == user_id and "university_id" in update_data:
        requested_university_id = update_data.get("university_id")
        if requested_university_id != db_user.university_id:
            raise HTTPException(
                status_code=400,
                detail="University affiliation cannot be changed from the profile page.",
            )

    if "university_id" in update_data and update_data["university_id"]:
        from app.models.university import University

        selected_university = (
            db.query(University)
            .filter(University.id == update_data["university_id"])
            .first()
        )
        if not selected_university:
            raise HTTPException(status_code=400, detail="Selected university was not found")

    for key, value in update_data.items():
        setattr(db_user, key, value)

    db.commit()
    db.refresh(db_user)
    return db_user


@router.delete("/{user_id}")
def delete_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to delete users")

    db_user = db.query(User).filter(User.id == user_id).first()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(db_user)
    db.commit()
    return {"detail": "User deleted"}


@router.post("/clear-cache")
def clear_cache():
    try:
        redis_client = get_redis()
        redis_client.flushdb()
        return {"detail": "Cache cleared successfully"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to clear cache: {exc}")
