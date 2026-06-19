"""Register, login, logout, and dashboard routes."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    clear_session_cookie,
    create_session,
    get_current_user,
    hash_password,
    require_login,
    revoke_session,
    set_session_cookie,
    verify_password,
)
from ..database import get_db
from ..models import Company, User, UserRole
from ..templates import templates

router = APIRouter()


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    """Compatibility wrapper: new Starlette takes request as first arg."""
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user)],
):
    if current_user:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "register.html", {"error": None})


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    name = name.strip()
    email = email.strip().lower()

    if not name or not email or not password:
        return _render(request, "register.html", {"error": "All fields are required."}, 400)

    if len(password) < 8:
        return _render(request, "register.html", {"error": "Password must be at least 8 characters."}, 400)

    if db.scalar(select(User).where(User.email == email)):
        return _render(request, "register.html", {"error": "An account with this email already exists."}, 400)

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_session(user, db)
    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token)
    return response


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user)],
):
    if current_user:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))

    if not user or not verify_password(password, user.password_hash):
        return _render(request, "login.html", {"error": "Invalid email or password."}, 401)

    token = create_session(user, db)
    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token)
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.post("/logout")
def logout(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    token = request.cookies.get("session")
    if token:
        revoke_session(token, db)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    company: Company | None = None
    if current_user.company_id:
        company = db.get(Company, current_user.company_id)

    return _render(request, "dashboard.html", {"user": current_user, "company": company})


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def root(current_user: Annotated[User | None, Depends(get_current_user)]):
    if current_user:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
