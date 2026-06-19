"""Authentication helpers: session management, password hashing, access guards."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta
from typing import Annotated

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import Company, User, UserRole, UserSession


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session token
# ---------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user: User, db: Session) -> str:
    """Create a new server-side session; return the raw bearer token."""
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=settings.session_ttl_hours)
    db.add(UserSession(
        user_id=user.id,
        token_hash=_token_hash(token),
        expires_at=expires,
    ))
    db.commit()
    return token


def revoke_session(token: str, db: Session) -> None:
    h = _token_hash(token)
    row = db.scalar(select(UserSession).where(UserSession.token_hash == h))
    if row:
        db.delete(row)
        db.commit()


def _resolve_session(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    h = _token_hash(token)
    row = db.scalar(
        select(UserSession)
        .where(UserSession.token_hash == h)
        .where(UserSession.expires_at > datetime.utcnow())
    )
    if row is None:
        return None
    return db.get(User, row.user_id)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    token = request.cookies.get("session")
    return _resolve_session(token, db)


def require_login(
    user: Annotated[User | None, Depends(get_current_user)],
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def require_admin(
    user: Annotated[User, Depends(require_login)],
) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")
    return user


# ---------------------------------------------------------------------------
# Company access guard
# ---------------------------------------------------------------------------

def user_can_access_company(user: User, company_id: int) -> bool:
    """Return True if the user is allowed to access the given company."""
    if user.role == UserRole.admin:
        return True
    return user.company_id == company_id


def require_company_access(user: User, company_id: int) -> None:
    """Raise 403 if the user cannot access the company."""
    if not user_can_access_company(user, company_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not assigned to this company.",
        )


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

SESSION_COOKIE = "session"
COOKIE_MAX_AGE = settings.session_ttl_hours * 3600


def _is_secure() -> bool:
    """True when HTTPS is detected via env var (Railway sets RAILWAY_ENVIRONMENT)."""
    import os
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("COOKIE_SECURE"))


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE)
