"""Admin routes: per-company reporting settings and KPI field management."""
from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import (
    Company, CompanyKpiField, CompanyReportingSettings,
    KpiFieldType, ReportingFrequency, User,
)
from ..standard_kpis import STANDARD_KPIS, STANDARD_KPI_KEYS
from ..templates import templates

router = APIRouter(prefix="/admin/companies")


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def _valid_field_key(key: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]*", key))


def _get_excluded(settings: CompanyReportingSettings) -> set[str]:
    if not settings.excluded_standard_kpis:
        return set()
    try:
        return set(json.loads(settings.excluded_standard_kpis))
    except Exception:
        return set()


def _set_excluded(settings: CompanyReportingSettings, excluded: set[str]) -> None:
    settings.excluded_standard_kpis = json.dumps(sorted(excluded)) if excluded else None


def _get_settings(db: Session, company_id: int) -> CompanyReportingSettings:
    s = db.scalar(
        select(CompanyReportingSettings).where(
            CompanyReportingSettings.company_id == company_id
        )
    )
    if not s:
        s = CompanyReportingSettings(
            company_id=company_id,
            reporting_frequency=ReportingFrequency.monthly,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Company settings page
# ---------------------------------------------------------------------------

@router.get("/{company_id}/settings", response_class=HTMLResponse)
def company_settings(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    error: str | None = None,
    success: str | None = None,
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    settings = _get_settings(db, company_id)
    kpi_fields = list(db.scalars(
        select(CompanyKpiField)
        .where(CompanyKpiField.company_id == company_id)
        .order_by(CompanyKpiField.id)
    ).all())

    excluded = _get_excluded(settings)

    return _render(request, "admin/company_settings.html", {
        "user": admin,
        "company": company,
        "settings": settings,
        "kpi_fields": kpi_fields,
        "frequencies": list(ReportingFrequency),
        "field_types": list(KpiFieldType),
        "standard_kpis": STANDARD_KPIS,
        "excluded_standard_kpis": excluded,
        "error": error,
        "success": success,
    })


# ---------------------------------------------------------------------------
# Update reporting settings
# ---------------------------------------------------------------------------

@router.post("/{company_id}/settings/reporting", response_class=HTMLResponse)
def update_reporting_settings(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    reporting_frequency: Annotated[str, Form()],
    reporting_start_date: Annotated[str, Form()] = "",
    reporting_day_due: Annotated[str, Form()] = "",
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    try:
        freq = ReportingFrequency(reporting_frequency)
    except ValueError:
        freq = ReportingFrequency.monthly

    start_date = None
    if reporting_start_date.strip():
        from datetime import date
        try:
            start_date = date.fromisoformat(reporting_start_date.strip())
        except ValueError:
            pass

    day_due = None
    if reporting_day_due.strip():
        try:
            d = int(reporting_day_due.strip())
            if 1 <= d <= 31:
                day_due = d
        except ValueError:
            pass

    s = _get_settings(db, company_id)
    s.reporting_frequency = freq
    s.reporting_start_date = start_date
    s.reporting_day_due = day_due
    db.commit()

    return RedirectResponse(
        f"/admin/companies/{company_id}/settings?success=Reporting+settings+saved.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Add KPI field
# ---------------------------------------------------------------------------

@router.post("/{company_id}/settings/kpi-fields/add", response_class=HTMLResponse)
def add_kpi_field(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    field_key: Annotated[str, Form()] = "",
    field_label: Annotated[str, Form()] = "",
    field_type: Annotated[str, Form()] = "text",
    description: Annotated[str, Form()] = "",
    extraction_hint: Annotated[str, Form()] = "",
    is_required: Annotated[str, Form()] = "",
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    field_key = field_key.strip().lower()
    field_label = field_label.strip()

    error = None
    if not field_key:
        error = "Field key is required."
    elif not _valid_field_key(field_key):
        error = "Field key must be lowercase letters, digits, and underscores, starting with a letter (e.g. revenue_eur)."
    elif not field_label:
        error = "Field label is required."
    else:
        existing = db.scalar(
            select(CompanyKpiField).where(
                CompanyKpiField.company_id == company_id,
                CompanyKpiField.field_key == field_key,
            )
        )
        if existing:
            error = f"A field with key '{field_key}' already exists for this company."

    if error:
        settings = _get_settings(db, company_id)
        kpi_fields = list(db.scalars(
            select(CompanyKpiField)
            .where(CompanyKpiField.company_id == company_id)
            .order_by(CompanyKpiField.id)
        ).all())
        return _render(request, "admin/company_settings.html", {
            "user": admin,
            "company": company,
            "settings": settings,
            "kpi_fields": kpi_fields,
            "frequencies": list(ReportingFrequency),
            "field_types": list(KpiFieldType),
            "standard_kpis": STANDARD_KPIS,
            "excluded_standard_kpis": _get_excluded(settings),
            "error": error,
            "success": None,
            "kpi_form": {
                "field_key": field_key,
                "field_label": field_label,
                "field_type": field_type,
                "description": description,
                "extraction_hint": extraction_hint,
                "is_required": is_required,
            },
        }, 400)

    try:
        ftype = KpiFieldType(field_type)
    except ValueError:
        ftype = KpiFieldType.text

    f = CompanyKpiField(
        company_id=company_id,
        field_key=field_key,
        field_label=field_label,
        field_type=ftype,
        description=description.strip() or None,
        extraction_hint=extraction_hint.strip() or None,
        is_required=bool(is_required),
        is_active=True,
    )
    db.add(f)
    db.commit()

    return RedirectResponse(
        f"/admin/companies/{company_id}/settings?success=KPI+field+added.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Toggle KPI field active/inactive
# ---------------------------------------------------------------------------

@router.post("/{company_id}/settings/kpi-fields/{field_id}/toggle")
def toggle_kpi_field(
    company_id: int,
    field_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    f = db.get(CompanyKpiField, field_id)
    if not f or f.company_id != company_id:
        raise HTTPException(status_code=404, detail="Field not found.")
    f.is_active = not f.is_active
    db.commit()
    return RedirectResponse(
        f"/admin/companies/{company_id}/settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Delete KPI field
# ---------------------------------------------------------------------------

@router.post("/{company_id}/settings/kpi-fields/{field_id}/delete")
def delete_kpi_field(
    company_id: int,
    field_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    f = db.get(CompanyKpiField, field_id)
    if not f or f.company_id != company_id:
        raise HTTPException(status_code=404, detail="Field not found.")
    db.delete(f)
    db.commit()
    return RedirectResponse(
        f"/admin/companies/{company_id}/settings?success=KPI+field+deleted.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Toggle standard KPI excluded/included
# ---------------------------------------------------------------------------

@router.post("/{company_id}/settings/standard-kpis/{kpi_key}/toggle")
def toggle_standard_kpi(
    company_id: int,
    kpi_key: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    if kpi_key not in STANDARD_KPI_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown standard KPI key: {kpi_key}")
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found.")
    settings = _get_settings(db, company_id)
    excluded = _get_excluded(settings)
    if kpi_key in excluded:
        excluded.discard(kpi_key)
    else:
        excluded.add(kpi_key)
    _set_excluded(settings, excluded)
    db.commit()
    return RedirectResponse(
        f"/admin/companies/{company_id}/settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )
