"""LP authentication: register, onboard, login, logout."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    hash_password,
    verify_password,
    set_session_cookie,
    clear_session_cookie,
    _token_hash,
)
from ..config import settings
from ..database import get_db
from ..models import LPUser, LPUserSession, LPInterestArea, LPLookingFor
from ..templates import templates
from datetime import datetime, timedelta
import secrets
import hashlib

log = logging.getLogger(__name__)
router = APIRouter(prefix="/lp")


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    resp = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    # Prevent the browser back/forward cache (bfcache) from showing an
    # authenticated page after logout. Forces a fresh request -> redirect to login.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _get_lp_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> LPUser | None:
    """Extract LP user from session cookie."""
    token = request.cookies.get("lp_session")
    if not token:
        return None
    
    h = _token_hash(token)
    row = db.scalar(
        select(LPUserSession)
        .where(LPUserSession.token_hash == h)
        .where(LPUserSession.expires_at > datetime.utcnow())
    )
    if row is None:
        return None
    return db.get(LPUser, row.lp_user_id)


def _lp_require_login(
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
) -> LPUser:
    """Require LP login, redirect to /lp/login if not authenticated."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/lp/login"}
        )
    return user


def _create_lp_session(lp_user: LPUser, db: Session) -> str:
    """Create a new server-side session for LP user; return the raw bearer token."""
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=settings.session_ttl_hours)
    db.add(LPUserSession(
        lp_user_id=lp_user.id,
        token_hash=_token_hash(token),
        expires_at=expires,
    ))
    db.commit()
    return token


def _set_lp_session_cookie(response, token: str) -> None:
    """Set LP session cookie."""
    from ..auth import _is_secure
    response.set_cookie(
        "lp_session",
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
    )


def _clear_lp_session_cookie(response) -> None:
    """Clear LP session cookie (match the path it was set with)."""
    response.delete_cookie("lp_session", path="/")


# ─────────────────────────────────────────────────────────────────────────────
# Register
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def lp_landing(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    """LP landing page. Redirects to dashboard if already logged in."""
    if current_user and current_user.onboarding_completed:
        return RedirectResponse("/lp/dashboard", status_code=303)

    from ..models import CrmVenture
    from sqlalchemy import select as _sel
    from sqlalchemy import func as _func

    portfolio = db.scalars(
        _sel(CrmVenture.name, CrmVenture.sector)
        .where(
            _func.lower(CrmVenture.stage) == "portfolio",
            CrmVenture.name.is_not(None),
        )
    ).all()
    portfolio_count = len(portfolio)

    # Build company list for marquee — use portfolio + any named active companies
    all_ventures = db.execute(
        _sel(CrmVenture.name, CrmVenture.sector).where(CrmVenture.name.is_not(None)).limit(60)
    ).all()
    companies = [{"name": r[0], "sector": r[1] or "Deep Tech"} for r in all_ventures if r[0]]

    return _render(request, "lp/landing.html", {
        "portfolio_count": portfolio_count,
        "companies": companies[:30],
    })


@router.get("/register", response_class=HTMLResponse)
def lp_register_page(
    request: Request,
    current_user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    """LP registration page."""
    if current_user:
        return RedirectResponse("/lp/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "lp/register.html", {"error": None})


@router.post("/register", response_class=HTMLResponse)
def lp_register(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    organization: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
):
    """LP registration form submission."""
    name = name.strip()
    email = email.strip().lower()
    organization = organization.strip()
    password = password.strip()

    # Validation
    if not name or not email or not password:
        return _render(request, "lp/register.html", {"error": "Name, email, and password are required."}, 400)

    if len(password) < 8:
        return _render(request, "lp/register.html", {"error": "Password must be at least 8 characters."}, 400)

    if db.scalar(select(LPUser).where(LPUser.email == email)):
        return _render(request, "lp/register.html", {"error": "An LP account with this email already exists."}, 400)

    # Create LP user
    lp_user = LPUser(
        name=name,
        email=email,
        organization=organization or None,
        password_hash=hash_password(password),
        onboarding_completed=False,
    )
    db.add(lp_user)
    db.commit()
    db.refresh(lp_user)

    # Create session and redirect to onboarding
    token = _create_lp_session(lp_user, db)
    response = RedirectResponse("/lp/onboard", status_code=status.HTTP_303_SEE_OTHER)
    _set_lp_session_cookie(response, token)
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/onboard", response_class=HTMLResponse)
def lp_onboard_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    """LP onboarding form (3-question questionnaire)."""
    if current_user is None:
        return RedirectResponse("/lp/login", status_code=status.HTTP_303_SEE_OTHER)
    
    if current_user.onboarding_completed:
        return RedirectResponse("/lp/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    interest_areas = [e.value for e in LPInterestArea]
    looking_for_options = [e.value for e in LPLookingFor]

    return _render(request, "lp/onboard.html", {
        "user": current_user,
        "interest_areas": interest_areas,
        "looking_for_options": looking_for_options,
    })


@router.post("/onboard", response_class=HTMLResponse)
def lp_onboard_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
    interest_areas: Annotated[str, Form()] = "",
    looking_for: Annotated[str, Form()] = "",
    about_yourself: Annotated[str, Form()] = "",
):
    """Process onboarding form submission."""
    if current_user is None:
        return RedirectResponse("/lp/login", status_code=status.HTTP_303_SEE_OTHER)

    # Parse multi-select fields (comma-separated)
    areas = [a.strip() for a in interest_areas.split(",") if a.strip()] if interest_areas else []
    looking = [l.strip() for l in looking_for.split(",") if l.strip()] if looking_for else []
    about = about_yourself.strip() if about_yourself else None

    # Update user
    current_user.interest_areas = json.dumps(areas) if areas else None
    current_user.looking_for = json.dumps(looking) if looking else None
    current_user.about_yourself = about
    current_user.onboarding_completed = True
    db.commit()

    log.info(f"LP user {current_user.email} completed onboarding")
    return RedirectResponse("/lp/dashboard", status_code=status.HTTP_303_SEE_OTHER)


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def lp_login_page(
    request: Request,
    current_user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    """LP login page."""
    if current_user:
        return RedirectResponse("/lp/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "lp/login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def lp_login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    """LP login form submission."""
    email = email.strip().lower()
    password = password.strip()

    lp_user = db.scalar(select(LPUser).where(LPUser.email == email))
    if not lp_user or not verify_password(password, lp_user.password_hash):
        return _render(request, "lp/login.html", {"error": "Invalid email or password."}, 401)

    # Create session
    token = _create_lp_session(lp_user, db)
    
    # Redirect to onboarding if not completed, otherwise dashboard
    next_url = "/lp/onboard" if not lp_user.onboarding_completed else "/lp/dashboard"
    response = RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)
    _set_lp_session_cookie(response, token)
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def lp_dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    """LP dashboard home."""
    if current_user is None:
        return RedirectResponse("/lp/login", status_code=status.HTTP_303_SEE_OTHER)

    # If onboarding not completed, redirect
    if not current_user.onboarding_completed:
        return RedirectResponse("/lp/onboard", status_code=status.HTTP_303_SEE_OTHER)

    return _render(request, "lp/dashboard.html", {
        "user": current_user,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Logout
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/logout")
def lp_logout(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """LP logout: invalidate the server-side session AND clear the cookie."""
    # Delete the server-side session row so the token can never be reused, even
    # if the cookie lingers anywhere. (Cookie clearing alone is not enough.)
    token = request.cookies.get("lp_session")
    if token:
        try:
            db.query(LPUserSession).filter(
                LPUserSession.token_hash == _token_hash(token)
            ).delete(synchronize_session=False)
            db.commit()
        except Exception as exc:
            log.warning("lp_logout: failed to delete session row: %s", exc)
            db.rollback()

    response = RedirectResponse("/lp/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_lp_session_cookie(response)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Export helper for LP chat routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/token-login")
def lp_token_login(
    token: str,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Exchange a Bearer token (from JS OTP flow) for a session cookie.
    Called after the React-style OTP registration succeeds.
    """
    from ..auth import _token_hash as _th
    h = _th(token)
    row = db.scalar(
        select(LPUserSession)
        .where(LPUserSession.token_hash == h)
        .where(LPUserSession.expires_at > datetime.utcnow())
    )
    if row is None:
        return RedirectResponse("/lp/login?error=invalid_token", status_code=303)
    user = db.get(LPUser, row.lp_user_id)
    if not user:
        return RedirectResponse("/lp/login", status_code=303)
    next_url = "/lp/onboard" if not user.onboarding_completed else "/lp/dashboard"
    response = RedirectResponse(next_url, status_code=303)
    _set_lp_session_cookie(response, token)
    return response


def require_lp_login(
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
) -> LPUser:
    """Dependency to require LP login."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/lp/login"}
        )
    return user