"""Document upload, listing, and detail routes."""
from __future__ import annotations

import hashlib
import logging
from typing import Annotated, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_login, user_can_access_company
from ..config import settings
from ..database import get_db
from ..models import (
    Company, Document, DocumentCategory, Extraction,
    ExtractionStatus, ReviewStatus, UploadStatus, User, UserRole,
)
from ..services.extraction_service import (
    ALLOWED_EXTENSIONS,
    extract_text,
    get_extension,
    is_allowed,
)
from ..templates import templates

log = logging.getLogger(__name__)
router = APIRouter()


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _readable_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _companies_for_user(user: User, db: Session) -> list[Company]:
    """Return companies the user is allowed to upload to."""
    if user.role == UserRole.admin:
        return list(db.scalars(select(Company).order_by(Company.name)).all())
    if user.company_id:
        c = db.get(Company, user.company_id)
        return [c] if c else []
    return []


# ---------------------------------------------------------------------------
# Bulk upload form
# ---------------------------------------------------------------------------

@router.get("/documents/upload/bulk", response_class=HTMLResponse)
def bulk_upload_form(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    companies = _companies_for_user(current_user, db)
    if not companies:
        return _render(request, "documents/upload_bulk.html",
                       {"user": current_user, "companies": [],
                        "allowed": sorted(ALLOWED_EXTENSIONS),
                        "error": "You are not assigned to any company."})
    return _render(request, "documents/upload_bulk.html", {
        "user": current_user,
        "companies": companies,
        "allowed": sorted(ALLOWED_EXTENSIONS),
        "error": None,
        "results": None,
    })


@router.post("/documents/upload/bulk", response_class=HTMLResponse)
async def bulk_upload_post(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    """
    Process a bulk upload form. Each file maps to a set of indexed fields:
      title_0, title_1, …
      document_category_0, document_category_1, …
      reporting_year_0, reporting_year_1, …
      reporting_month_0, reporting_month_1, …
      reporting_quarter_0, reporting_quarter_1, …
      company_id_0, company_id_1, …  (or single company_id for all)
    Files are submitted as files_0, files_1, … OR as a multi-file field 'files'.
    """
    import json as _json
    form = await request.form()
    companies = _companies_for_user(current_user, db)

    results: list[dict] = []

    # Count how many rows were submitted
    n = 0
    while f"title_{n}" in form or f"file_{n}" in form:
        n += 1

    if n == 0:
        return _render(request, "documents/upload_bulk.html", {
            "user": current_user,
            "companies": companies,
            "allowed": sorted(ALLOWED_EXTENSIONS),
            "error": "No files submitted.",
            "results": None,
        })

    for i in range(n):
        title = (form.get(f"title_{i}") or "").strip()
        document_category = (form.get(f"document_category_{i}") or "other").strip()
        reporting_year_raw = (form.get(f"reporting_year_{i}") or "").strip()
        reporting_month_raw = (form.get(f"reporting_month_{i}") or "").strip()
        reporting_quarter_raw = (form.get(f"reporting_quarter_{i}") or "").strip()

        # company: per-row or global fallback
        company_id_raw = (form.get(f"company_id_{i}") or form.get("company_id") or "").strip()
        try:
            company_id = int(company_id_raw)
        except (ValueError, TypeError):
            results.append({"index": i, "filename": f"row {i}", "ok": False, "error": "Missing company."})
            continue

        file: UploadFile | None = form.get(f"file_{i}")
        if file is None or not file.filename:
            results.append({"index": i, "filename": "—", "ok": False, "error": "No file."})
            continue

        filename = file.filename

        # Permission
        if not user_can_access_company(current_user, company_id):
            results.append({"index": i, "filename": filename, "ok": False, "error": "Not authorised for this company."})
            continue

        company = db.get(Company, company_id)
        if not company:
            results.append({"index": i, "filename": filename, "ok": False, "error": "Company not found."})
            continue

        if not title:
            results.append({"index": i, "filename": filename, "ok": False, "error": "Title is required."})
            continue

        if not is_allowed(filename):
            exts = ", ".join(f".{e}" for e in sorted(ALLOWED_EXTENSIONS))
            results.append({"index": i, "filename": filename, "ok": False, "error": f"File type not allowed. Accepted: {exts}"})
            continue

        data = await file.read()
        if not data:
            results.append({"index": i, "filename": filename, "ok": False, "error": "File is empty."})
            continue
        if len(data) > settings.max_upload_bytes:
            mb = settings.max_upload_bytes // (1024 * 1024)
            results.append({"index": i, "filename": filename, "ok": False, "error": f"File too large (max {mb} MB)."})
            continue

        # Category + period
        try:
            cat = DocumentCategory(document_category)
        except ValueError:
            cat = DocumentCategory.other

        is_regular = cat in (DocumentCategory.monthly_reporting, DocumentCategory.quarterly_reporting)
        r_year = r_month = r_quarter = None
        r_period = None

        if is_regular:
            try:
                r_year = int(reporting_year_raw)
                if not (2000 <= r_year <= 2100):
                    raise ValueError
            except (ValueError, TypeError):
                results.append({"index": i, "filename": filename, "ok": False, "error": "Valid reporting year required."})
                continue

            if cat == DocumentCategory.monthly_reporting:
                try:
                    r_month = int(reporting_month_raw)
                    if not (1 <= r_month <= 12):
                        raise ValueError
                except (ValueError, TypeError):
                    results.append({"index": i, "filename": filename, "ok": False, "error": "Valid reporting month required."})
                    continue
                r_period = f"{r_year}-{r_month:02d}"
            else:
                try:
                    r_quarter = int(reporting_quarter_raw)
                    if not (1 <= r_quarter <= 4):
                        raise ValueError
                except (ValueError, TypeError):
                    results.append({"index": i, "filename": filename, "ok": False, "error": "Valid reporting quarter required."})
                    continue
                r_period = f"{r_year}-Q{r_quarter}"

            # Duplicate period check
            from sqlalchemy import and_
            dup_q = select(Document).where(
                Document.company_id == company_id,
                Document.is_regular_reporting == True,  # noqa: E712
                Document.document_category == cat,
                Document.reporting_year == r_year,
            )
            if cat == DocumentCategory.monthly_reporting:
                dup_q = dup_q.where(Document.reporting_month == r_month)
            else:
                dup_q = dup_q.where(Document.reporting_quarter == r_quarter)
            if db.scalar(dup_q):
                results.append({"index": i, "filename": filename, "ok": False,
                                 "error": f"A {cat.value.replace('_',' ')} for {r_period} already exists for {company.name}."})
                continue

        # Duplicate file check
        digest = _sha256(data)
        if db.scalar(select(Document).where(Document.sha256 == digest, Document.company_id == company_id)):
            results.append({"index": i, "filename": filename, "ok": False, "error": "Duplicate file already uploaded for this company."})
            continue

        # Text extraction
        from ..services.extraction_service import extract_text, get_extension
        ext_str = get_extension(filename)
        raw_text = None
        extraction_error = None
        extraction_status = ExtractionStatus.complete
        try:
            raw_text = extract_text(filename, data)
        except Exception as exc:
            extraction_status = ExtractionStatus.failed
            extraction_error = str(exc)
        if raw_text is not None and len(raw_text.strip()) < 20:
            extraction_status = ExtractionStatus.failed
            extraction_error = "No usable text extracted."

        doc = Document(
            company_id=company_id,
            uploaded_by_id=current_user.id,
            title=title,
            filename=filename,
            file_type=ext_str,
            file_size=len(data),
            sha256=digest,
            file_bytes=data,  # keep original file so it can be downloaded/linked later
            raw_text=raw_text,
            upload_status=UploadStatus.uploaded,
            extraction_status=extraction_status,
            extraction_error=extraction_error,
            review_status=ReviewStatus.pending,
            document_category=cat,
            is_regular_reporting=is_regular,
            reporting_period=r_period,
            reporting_year=r_year,
            reporting_month=r_month,
            reporting_quarter=r_quarter,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        # LLM extraction (best-effort)
        if extraction_status == ExtractionStatus.complete and raw_text:
            from ..services.portfolio_extraction import run_portfolio_extraction
            from ..services.settings_service import get_openrouter_api_key
            api_key = get_openrouter_api_key(db)
            if api_key:
                try:
                    run_portfolio_extraction(doc.id, db)
                except Exception:
                    pass
                # Index full report text for portfolio chat retrieval (best-effort)
                try:
                    from ..services.embeddings import embed_document_raw_text
                    embed_document_raw_text(doc.id, db)
                except Exception as exc:
                    log.warning("raw-text embedding failed for document %d: %s", doc.id, exc)

        results.append({"index": i, "filename": filename, "ok": True, "doc_id": doc.id, "title": title})

    return _render(request, "documents/upload_bulk.html", {
        "user": current_user,
        "companies": companies,
        "allowed": sorted(ALLOWED_EXTENSIONS),
        "error": None,
        "results": results,
    })


# ---------------------------------------------------------------------------
# Document list
# ---------------------------------------------------------------------------

@router.get("/documents", response_class=HTMLResponse)
def documents_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    q = select(Document).order_by(Document.created_at.desc())
    if current_user.role != UserRole.admin:
        if not current_user.company_id:
            docs = []
        else:
            docs = list(db.scalars(q.where(Document.company_id == current_user.company_id)).all())
    else:
        docs = list(db.scalars(q).all())

    # Pre-load companies for display
    company_map: dict[int, Company] = {
        c.id: c for c in db.scalars(select(Company)).all()
    }

    return _render(request, "documents/list.html", {
        "user": current_user,
        "docs": docs,
        "company_map": company_map,
        "readable_size": _readable_size,
    })


# ---------------------------------------------------------------------------
# Upload form
# ---------------------------------------------------------------------------

def _upload_ctx(user, companies, error=None, form=None):
    return {
        "user": user,
        "companies": companies,
        "error": error,
        "allowed": sorted(ALLOWED_EXTENSIONS),
        "categories": [c.value for c in DocumentCategory],
        "form": form or {},
    }


@router.get("/documents/upload", response_class=HTMLResponse)
def upload_form(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    companies = _companies_for_user(current_user, db)
    if not companies:
        return _render(request, "documents/upload.html",
                       _upload_ctx(current_user, [],
                                   "You are not assigned to any company. Ask an admin to assign you before uploading."))
    return _render(request, "documents/upload.html", _upload_ctx(current_user, companies))


# ---------------------------------------------------------------------------
# Upload POST
# ---------------------------------------------------------------------------

@router.post("/documents/upload", response_class=HTMLResponse)
async def upload_document(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
    company_id: Annotated[int, Form()],
    title: Annotated[str, Form()] = "",
    file: Annotated[UploadFile, File()] = None,
    document_category: Annotated[str, Form()] = "other",
    reporting_year: Annotated[str, Form()] = "",
    reporting_month: Annotated[str, Form()] = "",
    reporting_quarter: Annotated[str, Form()] = "",
    redirect_to: Annotated[str, Form()] = "",
):
    companies = _companies_for_user(current_user, db)

    form_data = {
        "company_id": company_id, "title": title,
        "document_category": document_category,
        "reporting_year": reporting_year,
        "reporting_month": reporting_month,
        "reporting_quarter": reporting_quarter,
    }

    def _err(msg: str, code: int = 400):
        return _render(request, "documents/upload.html",
                       _upload_ctx(current_user, companies, msg, form_data), code)

    # ── Permission check ────────────────────────────────────────────────────
    if not user_can_access_company(current_user, company_id):
        return _err("You are not authorised to upload for this company.", 403)

    company = db.get(Company, company_id)
    if not company:
        return _err("Company not found.", 404)

    # ── Validate inputs ─────────────────────────────────────────────────────
    title = title.strip()
    if not title:
        return _err("Title is required.")

    if file is None or not file.filename:
        return _err("No file selected.")

    if not is_allowed(file.filename):
        exts = ", ".join(f".{e}" for e in sorted(ALLOWED_EXTENSIONS))
        return _err(f"File type not allowed. Accepted: {exts}")

    # ── Read file ────────────────────────────────────────────────────────────
    data = await file.read()

    if len(data) == 0:
        return _err("Uploaded file is empty.")

    if len(data) > settings.max_upload_bytes:
        mb = settings.max_upload_bytes // (1024 * 1024)
        return _err(f"File too large. Maximum size is {mb} MB.")

    # ── Category & period ────────────────────────────────────────────────────
    try:
        cat = DocumentCategory(document_category)
    except ValueError:
        cat = DocumentCategory.other

    is_regular = cat in (DocumentCategory.monthly_reporting, DocumentCategory.quarterly_reporting)
    r_year: int | None = None
    r_month: int | None = None
    r_quarter: int | None = None
    r_period: str | None = None

    if is_regular:
        try:
            r_year = int(reporting_year)
            if r_year < 2000 or r_year > 2100:
                raise ValueError
        except (ValueError, TypeError):
            return _err("A valid reporting year (e.g. 2024) is required for regular reportings.")

        if cat == DocumentCategory.monthly_reporting:
            try:
                r_month = int(reporting_month)
                if r_month < 1 or r_month > 12:
                    raise ValueError
            except (ValueError, TypeError):
                return _err("A valid reporting month (1–12) is required for monthly reportings.")
            r_period = f"{r_year}-{r_month:02d}"
        else:
            try:
                r_quarter = int(reporting_quarter)
                if r_quarter < 1 or r_quarter > 4:
                    raise ValueError
            except (ValueError, TypeError):
                return _err("A valid reporting quarter (1–4) is required for quarterly reportings.")
            r_period = f"{r_year}-Q{r_quarter}"

        # Check for duplicate reporting period
        from sqlalchemy import and_
        dup_q = select(Document).where(
            Document.company_id == company_id,
            Document.is_regular_reporting == True,  # noqa: E712
            Document.document_category == cat,
            Document.reporting_year == r_year,
        )
        if cat == DocumentCategory.monthly_reporting:
            dup_q = dup_q.where(Document.reporting_month == r_month)
        else:
            dup_q = dup_q.where(Document.reporting_quarter == r_quarter)
        dup = db.scalar(dup_q)
        if dup:
            return _err(
                f"A {cat.value.replace('_', ' ')} for {r_period} already exists "
                f"for this company (document #{dup.id}: \"{dup.title}\")."
            )

    # ── Duplicate detection ──────────────────────────────────────────────────
    digest = _sha256(data)
    existing = db.scalar(
        select(Document).where(
            Document.sha256 == digest,
            Document.company_id == company_id,
        )
    )
    if existing:
        return _err(
            f"This file has already been uploaded for {company.name} "
            f"(document #{existing.id}: \"{existing.title}\")."
        )

    # ── Step 1: Extract text ─────────────────────────────────────────────────
    ext = get_extension(file.filename)
    raw_text: str | None = None
    extraction_error: str | None = None
    extraction_status = ExtractionStatus.complete

    try:
        raw_text = extract_text(file.filename, data)
    except Exception as exc:
        log.warning("Text extraction failed for %s: %s", file.filename, exc)
        extraction_status = ExtractionStatus.failed
        extraction_error = str(exc)

    # Treat whitespace-only or very short text as unusable
    MIN_TEXT_CHARS = 20
    if raw_text is not None and len(raw_text.strip()) < MIN_TEXT_CHARS:
        raw_text = raw_text or ""
        extraction_status = ExtractionStatus.failed
        extraction_error = (
            "No usable text extracted from this document. "
            "Scanned PDFs and image-only files are not supported in the MVP — OCR is not available."
        )

    # ── Step 2: Persist document ─────────────────────────────────────────────
    doc = Document(
        company_id=company_id,
        uploaded_by_id=current_user.id,
        title=title,
        filename=file.filename,
        file_type=ext,
        file_size=len(data),
        sha256=digest,
        file_bytes=data,  # keep original file so it can be downloaded/linked later
        raw_text=raw_text,
        upload_status=UploadStatus.uploaded,
        extraction_status=extraction_status,
        extraction_error=extraction_error,
        review_status=ReviewStatus.pending,
        document_category=cat,
        is_regular_reporting=is_regular,
        reporting_period=r_period,
        reporting_year=r_year,
        reporting_month=r_month,
        reporting_quarter=r_quarter,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # ── Step 3: LLM extraction (synchronous, best-effort) ────────────────────
    # Only attempt if text extraction succeeded and produced usable content
    if extraction_status == ExtractionStatus.complete and raw_text:
        from ..services.portfolio_extraction import run_portfolio_extraction
        from ..services.settings_service import get_openrouter_api_key

        api_key = get_openrouter_api_key(db)
        if not api_key:
            doc.extraction_status = ExtractionStatus.no_api_key
            doc.extraction_error = (
                "OpenRouter API key is not configured. "
                "Set OPENROUTER_API_KEY in your environment or go to Admin → Settings."
            )
            db.commit()
        else:
            try:
                run_portfolio_extraction(doc.id, db)
            except Exception as exc:
                log.warning("LLM extraction failed for document %d: %s", doc.id, exc)
                # run_portfolio_extraction already updated doc status to failed
            # Index full report text for portfolio chat retrieval (best-effort)
            try:
                from ..services.embeddings import embed_document_raw_text
                embed_document_raw_text(doc.id, db)
            except Exception as exc:
                log.warning("raw-text embedding failed for document %d: %s", doc.id, exc)

    # Optional caller-provided return location (must be a local path)
    if redirect_to.startswith("/"):
        return RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/documents/{doc.id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Document detail
# ---------------------------------------------------------------------------

@router.get("/documents/{document_id}", response_class=HTMLResponse)
def document_detail(
    document_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    from sqlalchemy import func, select as _select
    from ..models import Chunk

    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    if not user_can_access_company(current_user, doc.company_id):
        raise HTTPException(status_code=403, detail="Access denied.")

    company = db.get(Company, doc.company_id)
    uploader = db.get(User, doc.uploaded_by_id)

    latest_extraction: Extraction | None = db.scalar(
        _select(Extraction)
        .where(Extraction.document_id == document_id)
        .order_by(Extraction.created_at.desc())
        .limit(1)
    )

    reviewed_by: User | None = None
    if latest_extraction and latest_extraction.reviewed_by_id:
        reviewed_by = db.get(User, latest_extraction.reviewed_by_id)

    chunk_count: int = db.scalar(
        _select(func.count()).select_from(Chunk)
        .where(Chunk.document_id == document_id, Chunk.approved == True)  # noqa: E712
    ) or 0

    return _render(request, "documents/detail.html", {
        "user": current_user,
        "doc": doc,
        "company": company,
        "uploader": uploader,
        "readable_size": _readable_size,
        "extraction": latest_extraction,
        "reviewed_by": reviewed_by,
        "chunk_count": chunk_count,
    })


# ---------------------------------------------------------------------------
# Download original file
# ---------------------------------------------------------------------------

_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
}


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    if not user_can_access_company(current_user, doc.company_id):
        raise HTTPException(status_code=403, detail="Access denied.")
    data = doc.file_bytes
    if not data:
        raise HTTPException(
            status_code=404,
            detail="Original file is not stored for this document (uploaded before originals were kept).",
        )
    mime = _MIME_BY_EXT.get((doc.file_type or "").lower(), "application/octet-stream")
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


# ---------------------------------------------------------------------------
# Trigger LLM extraction
# ---------------------------------------------------------------------------

@router.post("/documents/{document_id}/delete")
def delete_document(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    if not user_can_access_company(current_user, doc.company_id):
        raise HTTPException(status_code=403, detail="Access denied.")
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin only.")
    db.delete(doc)
    db.commit()
    return RedirectResponse("/documents", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/documents/{document_id}/extract")
def trigger_extraction(
    document_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    from ..services.portfolio_extraction import run_portfolio_extraction

    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    if not user_can_access_company(current_user, doc.company_id):
        raise HTTPException(status_code=403, detail="Access denied.")

    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin only.")

    try:
        run_portfolio_extraction(document_id, db)
    except (ValueError, RuntimeError) as exc:
        log.warning("Extraction failed for document %d: %s", document_id, exc)

    return RedirectResponse(f"/documents/{document_id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Re-embed approved extraction
# ---------------------------------------------------------------------------

@router.post("/documents/{document_id}/re-embed")
def re_embed_document(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    from sqlalchemy import select as _select
    from ..models import ExtractionJobStatus

    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin only.")

    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    extraction = db.scalar(
        _select(Extraction)
        .where(
            Extraction.document_id == document_id,
            Extraction.status == ExtractionJobStatus.approved,
        )
        .order_by(Extraction.created_at.desc())
        .limit(1)
    )

    from ..services.embeddings import embed_approved_extraction, embed_document_raw_text

    # Re-embed the approved KPI extraction, if any
    if extraction:
        try:
            embed_approved_extraction(extraction.id, db)
        except (ValueError, RuntimeError) as exc:
            log.warning("Re-embed failed for document %d: %s", document_id, exc)

    # Re-index the full report text for portfolio chat retrieval
    try:
        embed_document_raw_text(document_id, db)
    except (ValueError, RuntimeError) as exc:
        log.warning("Raw-text re-embed failed for document %d: %s", document_id, exc)

    return RedirectResponse(f"/documents/{document_id}", status_code=status.HTTP_303_SEE_OTHER)
