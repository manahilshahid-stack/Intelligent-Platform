"""Admin-only routes: overview, company management, user management."""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import Company, Document, Extraction, ExtractionJobStatus, User, UserRole
from ..templates import templates

router = APIRouter(prefix="/admin")


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80] or "company"


def _unique_slug(base: str, db: Session, exclude_id: int | None = None) -> str:
    slug = base
    n = 2
    while True:
        q = select(Company).where(Company.slug == slug)
        if exclude_id is not None:
            q = q.where(Company.id != exclude_id)
        if not db.scalar(q):
            return slug
        slug = f"{base}-{n}"
        n += 1


# ---------------------------------------------------------------------------
# Admin overview
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def admin_home(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    companies = db.scalars(select(Company)).all()
    users = db.scalars(select(User)).all()
    from sqlalchemy import func
    pending_reviews = db.scalar(
        select(func.count()).select_from(Extraction)
        .where(Extraction.status == ExtractionJobStatus.pending_review)
    ) or 0

    return _render(request, "admin/admin.html", {
        "user": admin,
        "companies": companies,
        "users": users,
        "pending_reviews": pending_reviews,
    })


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

@router.get("/companies", response_class=HTMLResponse)
def companies_page(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    companies = db.scalars(select(Company).order_by(Company.name)).all()
    return _render(request, "admin/companies.html", {
        "user": admin,
        "companies": companies,
        "error": None,
    })


@router.post("/companies", response_class=HTMLResponse)
def create_company(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
):
    name = name.strip()
    description = description.strip()

    if not name:
        companies = db.scalars(select(Company).order_by(Company.name)).all()
        return _render(request, "admin/companies.html", {
            "user": admin,
            "companies": companies,
            "error": "Company name is required.",
        }, 400)

    slug = _unique_slug(_slugify(name), db)
    company = Company(
        name=name,
        slug=slug,
        description=description or None,
    )
    db.add(company)
    db.commit()
    return RedirectResponse("/admin/companies", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    companies = db.scalars(select(Company).order_by(Company.name)).all()
    return _render(request, "admin/users.html", {
        "user": admin,
        "users": users,
        "companies": companies,
        "error": None,
        "success": None,
    })


@router.get("/companies/{company_id}/delete", response_class=HTMLResponse)
def delete_company_confirm(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    error: str | None = None,
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    from sqlalchemy import func
    from ..models import Document
    doc_count = db.scalar(
        select(func.count()).select_from(Document)
        .where(Document.company_id == company_id)
    ) or 0
    user_count = db.scalar(
        select(func.count()).select_from(User)
        .where(User.company_id == company_id)
    ) or 0

    return _render(request, "admin/delete_company.html", {
        "user": admin,
        "company": company,
        "doc_count": doc_count,
        "user_count": user_count,
        "error": error,
    })


@router.post("/companies/{company_id}/delete", response_class=HTMLResponse)
def delete_company(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    confirm_name: Annotated[str, Form()] = "",
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    if confirm_name.strip() != company.name:
        return RedirectResponse(
            f"/admin/companies/{company_id}/delete?error=Name+did+not+match.+Please+type+the+exact+company+name.",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    db.delete(company)
    db.commit()
    return RedirectResponse("/admin/companies", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/update", response_class=HTMLResponse)
def update_user(
    request: Request,
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    role: Annotated[str, Form()],
    company_id: Annotated[str, Form()] = "",
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Validate and apply role
    try:
        target.role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role!r}")

    # Validate and apply company assignment
    if company_id.strip():
        try:
            cid = int(company_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid company_id.")
        if not db.get(Company, cid):
            raise HTTPException(status_code=400, detail="Company not found.")
        target.company_id = cid
    else:
        target.company_id = None

    db.commit()
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)
