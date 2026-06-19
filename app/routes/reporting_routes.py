"""Reporting tracker routes: admin portfolio view + user company view."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_login
from ..database import get_db
from ..models import Company, CompanyReportingSettings, User, UserRole
from ..services.reporting_service import (
    TrackerRow, build_portfolio_tracker, build_rows_for_company,
    get_irregular_docs,
)
from ..templates import templates

router = APIRouter()


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ---------------------------------------------------------------------------
# Admin: portfolio-wide reporting tracker
# ---------------------------------------------------------------------------

@router.get("/admin/reporting-tracker", response_class=HTMLResponse)
def admin_reporting_tracker(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    company_id: int | None = None,
    year: int | None = None,
    period: str | None = None,
    filter_status: str | None = None,
):
    all_rows = build_portfolio_tracker(db)
    companies = list(db.scalars(select(Company).order_by(Company.name)).all())

    # Filter
    rows = all_rows
    if company_id:
        rows = [r for r in rows if r.company_id == company_id]
    if year:
        rows = [r for r in rows if r.year == year]
    if period:
        rows = [r for r in rows if r.period_label == period]
    if filter_status:
        rows = [r for r in rows if r.status == filter_status]

    # Available years for filter
    years = sorted({r.year for r in all_rows}, reverse=True)

    return _render(request, "admin/reporting_tracker.html", {
        "user": admin,
        "rows": rows,
        "companies": companies,
        "years": years,
        "filter_company_id": company_id,
        "filter_year": year,
        "filter_period": period,
        "filter_status": filter_status,
        "total": len(all_rows),
        "missing_count": sum(1 for r in all_rows if r.status == "missing"),
    })


# ---------------------------------------------------------------------------
# User: own-company reporting view
# ---------------------------------------------------------------------------

@router.get("/company/reporting", response_class=HTMLResponse)
def company_reporting(
    request: Request,
    current_user: Annotated[User, Depends(require_login)],
    db: Annotated[Session, Depends(get_db)],
):
    from fastapi import HTTPException
    from fastapi.responses import RedirectResponse

    # Admins can use the admin tracker; redirect them
    if current_user.role == UserRole.admin:
        return RedirectResponse("/admin/reporting-tracker")

    if not current_user.company_id:
        from ..templates import templates as _t
        return _t.TemplateResponse(request, "company/reporting.html", {
            "request": request,
            "user": current_user,
            "company": None,
            "rows": [],
            "irregular_docs": [],
            "error": "You are not assigned to a company. Ask an admin to assign you.",
        })

    company = db.get(Company, current_user.company_id)
    if not company:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Company not found.")

    settings = db.scalar(
        select(CompanyReportingSettings).where(
            CompanyReportingSettings.company_id == company.id
        )
    )

    rows: list[TrackerRow] = []
    if settings:
        rows = build_rows_for_company(company, settings, db)

    irregular_docs = get_irregular_docs(company.id, db)

    return _render(request, "company/reporting.html", {
        "user": current_user,
        "company": company,
        "settings": settings,
        "rows": rows,
        "irregular_docs": irregular_docs,
        "error": None,
    })
