"""Reporting tracker routes: admin portfolio view + user company view."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
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
    year: int | None = None,
    quarter: int | None = None,
    message: str | None = None,
    error: str | None = None,
):
    from ..services.email_service import smtp_configured
    from ..services.reminder_service import (
        current_collection_period, due_date, get_founder_contacts,
        recent_periods, reminder1_date, status_for,
    )

    cur_year, cur_q = current_collection_period()
    year = year or cur_year
    quarter = quarter or cur_q

    companies = list(db.scalars(select(Company).order_by(Company.name)).all())
    rows = []
    for c in companies:
        state = status_for(db, c, year, quarter)
        rows.append({
            "company": c,
            "founders": get_founder_contacts(c, db),
            **state,
        })

    uploaded = sum(1 for r in rows if r["status"] == "uploaded")

    return _render(request, "admin/reporting_tracker.html", {
        "user": admin,
        "rows": rows,
        "year": year,
        "quarter": quarter,
        "periods": recent_periods(6),
        "due": due_date(year, quarter),
        "r1_date": reminder1_date(year, quarter),
        "uploaded": uploaded,
        "total": len(rows),
        "smtp_ok": smtp_configured(),
        "message": message,
        "error": error,
    })


@router.post("/admin/reporting-tracker/sweep", response_class=HTMLResponse)
def reminder_sweep_now(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from urllib.parse import quote
    from fastapi import status as _status
    from fastapi.responses import RedirectResponse
    from ..services.reminder_service import run_reminder_sweep

    result = run_reminder_sweep(db)
    return RedirectResponse(
        f"/admin/reporting-tracker?message={quote(result['message'])}",
        status_code=_status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/reporting-tracker/{company_id}/send", response_class=HTMLResponse)
def send_reminder_now(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    year: int = Form(...),
    quarter: int = Form(...),
    which: int = Form(...),
):
    from urllib.parse import quote
    from fastapi import HTTPException
    from fastapi import status as _status
    from fastapi.responses import RedirectResponse
    from ..services.reminder_service import send_reminder

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    result = send_reminder(db, company, year, quarter, which=which)
    param = "message" if result["ok"] else "error"
    return RedirectResponse(
        f"/admin/reporting-tracker?year={year}&quarter={quarter}&{param}={quote(result['message'])}",
        status_code=_status.HTTP_303_SEE_OTHER,
    )


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


# ---------------------------------------------------------------------------
# Fund reports: the condensed quarterly report built from all company reports
# ---------------------------------------------------------------------------

def _quarter_submissions(db: Session, year: int, quarter: int) -> tuple[set[int], dict[int, "Document"]]:
    """Company ids that submitted a quarterly report for the period, + their docs."""
    from ..models import Document, DocumentCategory
    docs = db.scalars(
        select(Document).where(
            Document.is_regular_reporting == True,  # noqa: E712
            Document.document_category == DocumentCategory.quarterly_reporting,
            Document.reporting_year == year,
            Document.reporting_quarter == quarter,
        )
    ).all()
    by_company: dict[int, Document] = {}
    for d in docs:
        by_company.setdefault(d.company_id, d)
    return set(by_company), by_company


@router.get("/admin/fund-reports", response_class=HTMLResponse)
def fund_reports_list(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    error: str | None = None,
):
    from datetime import date
    from ..models import FundReport

    reports = list(db.scalars(
        select(FundReport).order_by(FundReport.year.desc(), FundReport.quarter.desc())
    ).all())
    companies = list(db.scalars(select(Company).order_by(Company.name)).all())
    total_companies = len(companies)

    rows = []
    for r in reports:
        submitted, _ = _quarter_submissions(db, r.year, r.quarter)
        rows.append({
            "report": r,
            "submitted_count": len(submitted),
            "total": total_companies,
        })

    today = date.today()
    current_q = (today.month - 1) // 3 + 1

    return _render(request, "admin/fund_reports.html", {
        "user": admin,
        "rows": rows,
        "error": error,
        "default_year": today.year,
        "default_quarter": current_q,
    })


@router.post("/admin/fund-reports", response_class=HTMLResponse)
def fund_report_create(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    year: int = Form(...),
    quarter: int = Form(...),
):
    from fastapi.responses import RedirectResponse
    from fastapi import status as _status
    from ..models import FundReport

    if not (1 <= quarter <= 4) or not (2000 <= year <= 2100):
        return RedirectResponse(
            "/admin/fund-reports?error=Invalid+period.", status_code=_status.HTTP_303_SEE_OTHER
        )
    existing = db.scalar(
        select(FundReport).where(FundReport.year == year, FundReport.quarter == quarter)
    )
    if existing:
        return RedirectResponse(
            f"/admin/fund-reports/{existing.id}", status_code=_status.HTTP_303_SEE_OTHER
        )
    report = FundReport(year=year, quarter=quarter)
    db.add(report)
    db.commit()
    return RedirectResponse(
        f"/admin/fund-reports/{report.id}", status_code=_status.HTTP_303_SEE_OTHER
    )


@router.get("/admin/fund-reports/{report_id}", response_class=HTMLResponse)
def fund_report_detail(
    report_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    success: str | None = None,
):
    from fastapi import HTTPException
    from ..models import FundReport, FundReportStatus

    report = db.get(FundReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Fund report not found.")

    companies = list(db.scalars(select(Company).order_by(Company.name)).all())
    submitted_ids, docs_by_company = _quarter_submissions(db, report.year, report.quarter)

    company_rows = [
        {
            "company": c,
            "submitted": c.id in submitted_ids,
            "document": docs_by_company.get(c.id),
        }
        for c in companies
    ]

    return _render(request, "admin/fund_report_detail.html", {
        "user": admin,
        "report": report,
        "company_rows": company_rows,
        "submitted_count": len(submitted_ids),
        "total": len(companies),
        "statuses": [s.value for s in FundReportStatus],
        "success": success,
    })


@router.post("/admin/fund-reports/{report_id}/update", response_class=HTMLResponse)
def fund_report_update(
    report_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    status: str = Form(""),
    notes: str = Form(""),
    final_document_id: str = Form(""),
):
    from fastapi import HTTPException
    from fastapi import status as _status
    from fastapi.responses import RedirectResponse
    from ..models import Document, FundReport, FundReportStatus

    report = db.get(FundReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Fund report not found.")

    if status:
        try:
            report.status = FundReportStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status!r}")
    report.notes = notes.strip() or None
    if final_document_id.strip():
        try:
            doc_id = int(final_document_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid document id.")
        if not db.get(Document, doc_id):
            raise HTTPException(status_code=400, detail="Document not found.")
        report.final_document_id = doc_id
    else:
        report.final_document_id = None
    db.commit()
    return RedirectResponse(
        f"/admin/fund-reports/{report_id}?success=Saved.",
        status_code=_status.HTTP_303_SEE_OTHER,
    )
