"""Admin review/correction/approval routes for LLM extractions."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import (
    Company, CompanyKpiField, CompanyReportingSettings, Document, Extraction,
    ExtractionJobStatus, ExtractionStatus, ReviewStatus, User,
)
from ..standard_kpis import STANDARD_KPIS
from ..templates import templates

log = logging.getLogger(__name__)
router = APIRouter()


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_data(extraction: Extraction) -> dict:
    """Return parsed extraction JSON, preferring corrected over raw."""
    src = extraction.corrected_json or extraction.extracted_json or "{}"
    try:
        return json.loads(src)
    except Exception:
        return {}


def _field(data: dict, *keys, default=""):
    """Safe nested get with empty-string default."""
    v = data
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k)
        if v is None:
            return default
    return "" if v is None else v


def _list_field(data: dict, key: str) -> str:
    """Return list field as newline-separated string."""
    items = data.get(key)
    if not isinstance(items, list):
        return ""
    return "\n".join(str(i) for i in items if i)


def _parse_list(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _parse_number(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Review list  GET /admin/review
# ---------------------------------------------------------------------------

@router.get("/admin/review", response_class=HTMLResponse)
def review_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    extractions = list(db.scalars(
        select(Extraction)
        .where(Extraction.status == ExtractionJobStatus.pending_review)
        .order_by(Extraction.created_at.desc())
    ).all())

    rows = []
    for ex in extractions:
        doc = db.get(Document, ex.document_id)
        company = db.get(Company, ex.company_id) if ex.company_id else None
        data = _parse_data(ex)
        rows.append({
            "extraction": ex,
            "doc": doc,
            "company": company,
            "confidence": data.get("confidence") or "—",
            "missing_fields": data.get("missing_fields") or [],
        })

    return _render(request, "admin/review_list.html", {
        "user": current_user,
        "rows": rows,
    })


# ---------------------------------------------------------------------------
# Review detail  GET /admin/review/{extraction_id}
# ---------------------------------------------------------------------------

@router.get("/admin/review/{extraction_id}", response_class=HTMLResponse)
def review_detail(
    extraction_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    extraction = db.get(Extraction, extraction_id)
    if not extraction:
        raise HTTPException(status_code=404, detail="Extraction not found.")

    doc = db.get(Document, extraction.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    company = db.get(Company, doc.company_id)
    data = _parse_data(extraction)

    kpi_fields = list(db.scalars(
        select(CompanyKpiField)
        .where(
            CompanyKpiField.company_id == doc.company_id,
            CompanyKpiField.is_active == True,  # noqa: E712
        )
        .order_by(CompanyKpiField.id)
    ).all())

    # Load excluded standard KPIs for this company
    import json as _json
    settings = db.scalar(
        select(CompanyReportingSettings).where(
            CompanyReportingSettings.company_id == doc.company_id
        )
    )
    excluded_standard: set[str] = set()
    if settings and settings.excluded_standard_kpis:
        try:
            excluded_standard = set(_json.loads(settings.excluded_standard_kpis))
        except Exception:
            pass

    return _render(request, "admin/review_detail.html", {
        "user": current_user,
        "extraction": extraction,
        "doc": doc,
        "company": company,
        "data": data,
        "kpi_fields": kpi_fields,
        "standard_kpis": STANDARD_KPIS,
        "excluded_standard_kpis": excluded_standard,
        "f": _field,
        "lf": _list_field,
        "raw_json": json.dumps(data, indent=2, ensure_ascii=False),
    })


# ---------------------------------------------------------------------------
# Approve  POST /admin/review/{extraction_id}/approve
# ---------------------------------------------------------------------------

@router.post("/admin/review/{extraction_id}/approve")
async def approve_extraction(
    extraction_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    from datetime import datetime

    extraction = db.get(Extraction, extraction_id)
    if not extraction:
        raise HTTPException(status_code=404, detail="Extraction not found.")

    doc = db.get(Document, extraction.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    form = await request.form()

    def g(key: str, default: str = "") -> str:
        return (form.get(key) or default).strip()

    # ── Build custom_kpis from form fields ──────────────────────────────────
    kpi_fields = list(db.scalars(
        select(CompanyKpiField)
        .where(
            CompanyKpiField.company_id == doc.company_id,
            CompanyKpiField.is_active == True,  # noqa: E712
        )
        .order_by(CompanyKpiField.id)
    ).all())

    # Preserve any existing custom_kpis from extracted_json as baseline
    baseline_data = _parse_data(extraction)
    baseline_custom = baseline_data.get("custom_kpis") or {}

    custom_kpis: dict = {}
    for kf in kpi_fields:
        fk = kf.field_key
        raw_val = g(f"ckpi_{fk}_value")
        raw_src = g(f"ckpi_{fk}_source")
        raw_conf = g(f"ckpi_{fk}_confidence") or "medium"

        # Type-coerce value
        if raw_val == "":
            typed_val = None
        elif kf.field_type.value in ("number", "currency", "percentage"):
            typed_val = _parse_number(raw_val)
        elif kf.field_type.value == "boolean":
            typed_val = raw_val.lower() in ("true", "yes", "1") if raw_val else None
        else:
            typed_val = raw_val or None

        custom_kpis[fk] = {
            "label": kf.field_label,
            "value": typed_val,
            "type": kf.field_type.value,
            "source_text": raw_src or None,
            "confidence": raw_conf if raw_conf in ("low", "medium", "high") else "medium",
        }

    # Include any keys from the extracted JSON that are no longer active fields
    for fk, entry in baseline_custom.items():
        if fk not in custom_kpis:
            custom_kpis[fk] = entry

    corrected: dict = {
        "company_name": g("company_name") or None,
        "document_title": g("document_title") or None,
        "period": g("period") or None,
        "cash_position": {
            "value": _parse_number(g("cash_position_value")),
            "currency": g("cash_position_currency") or None,
            "source_text": g("cash_position_source") or None,
        },
        "monthly_burn": {
            "value": _parse_number(g("monthly_burn_value")),
            "currency": g("monthly_burn_currency") or None,
            "source_text": g("monthly_burn_source") or None,
        },
        "runway_months": {
            "value": _parse_number(g("runway_months_value")),
            "source_text": g("runway_months_source") or None,
        },
        "revenue": {
            "value": _parse_number(g("revenue_value")),
            "currency": g("revenue_currency") or None,
            "period": g("revenue_period") or None,
            "source_text": g("revenue_source") or None,
        },
        "arr": {
            "value": _parse_number(g("arr_value")),
            "currency": g("arr_currency") or None,
            "source_text": g("arr_source") or None,
        },
        "headcount": {
            "value": _parse_number(g("headcount_value")),
            "source_text": g("headcount_source") or None,
        },
        "customers": {
            "value": _parse_number(g("customers_value")),
            "source_text": g("customers_source") or None,
        },
        "key_wins":            _parse_list(g("key_wins")),
        "key_challenges":      _parse_list(g("key_challenges")),
        "risks":               _parse_list(g("risks")),
        "asks":                _parse_list(g("asks")),
        "next_milestones":     _parse_list(g("next_milestones")),
        "business_description": g("business_description") or None,
        "mrr": {
            "value": _parse_number(g("mrr_value")),
            "currency": g("mrr_currency") or None,
            "source_text": g("mrr_source") or None,
        },
        "gross_margin": {
            "value": _parse_number(g("gross_margin_value")),
            "source_text": g("gross_margin_source") or None,
        },
        "growth_metrics": {
            "mom_growth_pct": _parse_number(g("mom_growth_pct")),
            "yoy_growth_pct": _parse_number(g("yoy_growth_pct")),
            "description": g("growth_description") or None,
            "source_text": g("growth_source") or None,
        },
        "summary":        g("summary") or None,
        "confidence":     g("confidence") or None,
        "missing_fields": _parse_list(g("missing_fields")),
        "custom_kpis":    custom_kpis,
    }

    extraction.corrected_json = json.dumps(corrected, ensure_ascii=False)
    extraction.status = ExtractionJobStatus.approved
    extraction.reviewed_by_id = current_user.id
    extraction.reviewed_at = datetime.utcnow()

    doc.extraction_status = ExtractionStatus.extracted
    doc.review_status = ReviewStatus.approved
    db.commit()

    # Embed the corrected extraction (best-effort; failure does not block approval)
    from ..services.embeddings import embed_approved_extraction
    try:
        n = embed_approved_extraction(extraction_id, db)
        log.info("Extraction %d approved by user %d — %d chunk(s) embedded",
                 extraction_id, current_user.id, n)
    except Exception as exc:
        log.warning("Embedding failed for extraction %d (approved anyway): %s",
                    extraction_id, exc)

    return RedirectResponse(f"/documents/{doc.id}", status_code=303)


# ---------------------------------------------------------------------------
# Reject  POST /admin/review/{extraction_id}/reject
# ---------------------------------------------------------------------------

@router.post("/admin/review/{extraction_id}/reject")
def reject_extraction(
    extraction_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    from datetime import datetime

    extraction = db.get(Extraction, extraction_id)
    if not extraction:
        raise HTTPException(status_code=404, detail="Extraction not found.")

    doc = db.get(Document, extraction.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    extraction.status = ExtractionJobStatus.rejected
    extraction.reviewed_by_id = current_user.id
    extraction.reviewed_at = datetime.utcnow()
    doc.review_status = ReviewStatus.rejected

    db.commit()
    log.info("Extraction %d rejected by user %d", extraction_id, current_user.id)
    return RedirectResponse(f"/documents/{doc.id}", status_code=303)
