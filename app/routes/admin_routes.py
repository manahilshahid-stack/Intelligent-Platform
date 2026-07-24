"""Admin-only routes: overview, company management, user management."""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
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

def _companies_overview(db: Session) -> list[dict]:
    """Companies with doc counts + latest report period, for the buckets list."""
    from sqlalchemy import func
    from ..models import DocumentCategory

    companies = db.scalars(select(Company).order_by(Company.name)).all()
    doc_counts = dict(
        db.execute(
            select(Document.company_id, func.count(Document.id))
            .group_by(Document.company_id)
        ).all()
    )
    # Latest regular-reporting doc per company
    latest_reports: dict[int, Document] = {}
    for doc in db.scalars(
        select(Document)
        .where(Document.is_regular_reporting == True)  # noqa: E712
        .order_by(Document.reporting_year, Document.reporting_quarter,
                  Document.reporting_month)
    ):
        latest_reports[doc.company_id] = doc  # last write wins = latest period

    return [
        {
            "company": c,
            "doc_count": doc_counts.get(c.id, 0),
            "latest_report": latest_reports.get(c.id),
        }
        for c in companies
    ]


@router.get("/companies", response_class=HTMLResponse)
def companies_page(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    return _render(request, "admin/companies.html", {
        "user": admin,
        "rows": _companies_overview(db),
        "error": None,
    })


@router.post("/companies", response_class=HTMLResponse)
def create_company(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    drive_folder_url: Annotated[str, Form()] = "",
):
    name = name.strip()
    description = description.strip()
    drive_folder_url = drive_folder_url.strip()

    if not name:
        return _render(request, "admin/companies.html", {
            "user": admin,
            "rows": _companies_overview(db),
            "error": "Company name is required.",
        }, 400)

    slug = _unique_slug(_slugify(name), db)
    company = Company(
        name=name,
        slug=slug,
        description=description or None,
        drive_folder_url=drive_folder_url or None,
    )
    db.add(company)
    db.commit()
    return RedirectResponse(f"/admin/companies/{company.id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Company bucket (detail) page
# ---------------------------------------------------------------------------

@router.get("/companies/{company_id}", response_class=HTMLResponse)
def company_bucket(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    error: str | None = None,
    success: str | None = None,
):
    from ..models import CompanyReportingSettings, DocumentCategory
    from ..services.reminder_service import get_founder_contacts
    from ..services.reporting_service import build_rows_for_company, get_irregular_docs

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    docs = list(db.scalars(
        select(Document)
        .where(Document.company_id == company_id)
        .order_by(
            Document.reporting_year.desc().nulls_last(),
            Document.reporting_quarter.desc().nulls_last(),
            Document.reporting_month.desc().nulls_last(),
            Document.created_at.desc(),
        )
    ).all())

    # Group: regular reporting docs by period label, the rest under "Other documents"
    groups: dict[str, list[Document]] = {}
    for d in docs:
        if d.is_regular_reporting and d.reporting_period:
            key = d.reporting_period
        else:
            key = "Other documents"
        groups.setdefault(key, []).append(d)
    # Keep "Other documents" last
    other = groups.pop("Other documents", None)
    grouped = list(groups.items())
    if other:
        grouped.append(("Other documents", other))

    # Reporting completeness (uses per-company reporting settings if present)
    settings = db.scalar(
        select(CompanyReportingSettings)
        .where(CompanyReportingSettings.company_id == company_id)
    )
    tracker_rows = build_rows_for_company(company, settings, db) if settings else []

    from datetime import date as _date
    return _render(request, "admin/company_bucket.html", {
        "user": admin,
        "now_year": _date.today().year,
        "founders": get_founder_contacts(company, db),
        "company": company,
        "grouped_docs": grouped,
        "doc_count": len(docs),
        "tracker_rows": tracker_rows,
        "has_settings": settings is not None,
        "categories": [c.value for c in DocumentCategory],
        "error": error,
        "success": success,
    })


def _sync_drive_background(company_id: int, uploaded_by_id: int) -> None:
    """Drive folder sync as a background task with its own DB session —
    downloading + LLM-processing many files far exceeds the request timeout."""
    import logging
    log = logging.getLogger(__name__)
    from ..database import SessionLocal
    from ..services.gdrive_ingest import sync_company_drive_folder

    db = SessionLocal()
    try:
        company = db.get(Company, company_id)
        if not company:
            return
        result = sync_company_drive_folder(company, db, uploaded_by_id=uploaded_by_id)
        log.info("drive sync (company %d): %s", company_id, result.get("message"))
    except Exception as exc:
        log.error("drive sync (company %d) crashed: %s", company_id, exc)
    finally:
        db.close()


@router.post("/companies/{company_id}/sync-drive", response_class=HTMLResponse)
def sync_company_drive(
    company_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from ..services.gdrive_ingest import _FOLDER_ID_RE, _service_account_token

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    # Validate cheaply up-front so the user gets immediate feedback;
    # the actual download/processing runs in the background.
    if not company.drive_folder_url or not _FOLDER_ID_RE.search(company.drive_folder_url):
        return RedirectResponse(
            f"/admin/companies/{company_id}?error=No+valid+Drive+folder+URL+linked.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not _service_account_token(db):
        return RedirectResponse(
            f"/admin/companies/{company_id}?error=Google+service+account+is+not+configured+"
            f"(set+GOOGLE_SERVICE_ACCOUNT_JSON+and+share+the+folder+with+it).",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    background_tasks.add_task(_sync_drive_background, company_id, admin.id)
    return RedirectResponse(
        f"/admin/companies/{company_id}?success=Drive+sync+started+—+new+files+will+appear+here+in+a+few+minutes.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/companies/import-from-crm", response_class=HTMLResponse)
def import_companies_from_crm(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    """Create a portal company for every Attio portfolio venture (stage=Portfolio)
    that doesn't exist yet, and auto-fill founder contacts from Attio."""
    from urllib.parse import quote
    from ..models import CrmVenture
    from ..services.reminder_service import refresh_founders_from_attio

    ventures = db.scalars(
        select(CrmVenture).where(CrmVenture.stage.ilike("portfolio"))
        .order_by(CrmVenture.name)
    ).all()
    if not ventures:
        return RedirectResponse(
            "/admin/companies?error=" + quote("No portfolio ventures found in the CRM sync."),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    existing_names = {c.name.strip().lower() for c in db.scalars(select(Company)).all()}
    created = founders_filled = 0
    for v in ventures:
        name = (v.name or "").strip()
        if not name or name.lower() in existing_names:
            continue
        company = Company(
            name=name,
            slug=_unique_slug(_slugify(name), db),
            description=(v.description or None) if hasattr(v, "description") else None,
        )
        db.add(company)
        db.commit()
        created += 1
        existing_names.add(name.lower())
        result = refresh_founders_from_attio(company, db)
        if result.get("ok"):
            founders_filled += 1

    return RedirectResponse(
        "/admin/companies?success=" + quote(
            f"Imported {created} portfolio companies from CRM "
            f"({founders_filled} with founder contacts from Attio)."
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/companies/{company_id}/refresh-founders", response_class=HTMLResponse)
def refresh_founders(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from urllib.parse import quote
    from ..services.reminder_service import refresh_founders_from_attio

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")
    result = refresh_founders_from_attio(company, db)
    param = "success" if result.get("ok") else "error"
    return RedirectResponse(
        f"/admin/companies/{company_id}?{param}={quote(result['message'])}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/companies/{company_id}/founders", response_class=HTMLResponse)
def update_founders(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    founders_text: Annotated[str, Form()] = "",
):
    """Save founder contacts. One per line: 'Name <email@company.com>' or 'Name'."""
    import json as _json
    import re as _re

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    contacts = []
    for line in founders_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _re.match(r"^(.*?)[<\s]*([\w.+-]+@[\w-]+\.[\w.-]+)?[>\s]*$", line)
        name = (m.group(1) if m else line).strip(" <>,-")
        email = (m.group(2) or "").strip() if m else ""
        if name or email:
            contacts.append({"name": name or email, "email": email})

    company.founder_contacts = _json.dumps(contacts, ensure_ascii=False) if contacts else None
    db.commit()
    return RedirectResponse(
        f"/admin/companies/{company_id}?success=Founder+contacts+saved.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/companies/{company_id}/update", response_class=HTMLResponse)
def update_company(
    company_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    drive_folder_url: Annotated[str, Form()] = "",
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    name = name.strip()
    if name and name != company.name:
        company.name = name
        company.slug = _unique_slug(_slugify(name), db, exclude_id=company_id)
    company.description = description.strip() or None
    company.drive_folder_url = drive_folder_url.strip() or None
    db.commit()
    return RedirectResponse(
        f"/admin/companies/{company_id}?success=Company+updated.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
