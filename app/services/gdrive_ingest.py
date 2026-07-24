"""
Google Drive / Docs ingestion (Phase 3a).

Your CRM stores documents as *links* (Google Docs/Slides/Sheets/Drive files) inside
notes and record fields. This module:

  1. discovers those links across crm_notes + crm_ventures,
  2. fetches the actual document content (public links work with no auth; private
     files use a Google service-account key if configured),
  3. extracts text, chunks it, embeds it, and stores it in knowledge_chunks with
     source_type='gdrive' — so it flows through the normal retrieval + role-gating
     + sanitization pipeline (admins see full text, non-admins the sanitized copy).

Network calls go through httpx. Everything is fail-safe: a link that can't be
read is recorded with a status and skipped — it never breaks ingestion or chat.

Service-account auth (optional, for PRIVATE docs):
  Set app setting / env `GOOGLE_SERVICE_ACCOUNT_JSON` to the service-account key
  JSON, and share the Drive folder(s)/files with that service account's email.
  Requires the `google-auth` package (optional import).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 60
_MAX_TEXT = 200_000  # cap extracted text per document

# ---------------------------------------------------------------------------
# Link detection
# ---------------------------------------------------------------------------

# Ordered: published links first (they contain /d/e/), then normal /d/<id>.
_PATTERNS = [
    ("pub",         re.compile(r"https?://docs\.google\.com/(?:document|presentation|spreadsheets)/d/e/([\w-]+)/pub[\w?=&.-]*", re.I)),
    ("doc",         re.compile(r"https?://docs\.google\.com/document/d/(?!e/)([\w-]+)", re.I)),
    ("slides",      re.compile(r"https?://docs\.google\.com/presentation/d/(?!e/)([\w-]+)", re.I)),
    ("sheet",       re.compile(r"https?://docs\.google\.com/spreadsheets/d/(?!e/)([\w-]+)", re.I)),
    ("drive_file",  re.compile(r"https?://drive\.google\.com/file/d/([\w-]+)", re.I)),
    ("drive_open",  re.compile(r"https?://drive\.google\.com/open\?id=([\w-]+)", re.I)),
    ("folder",      re.compile(r"https?://drive\.google\.com/drive/folders/([\w-]+)", re.I)),
]


def extract_drive_links(text: str) -> list[dict]:
    """Return [{kind, file_id, url}] for every Google link found in *text* (deduped by url)."""
    if not text:
        return []
    found: dict[str, dict] = {}
    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            url = m.group(0)
            file_id = m.group(1)
            # don't let a normal /d/ pattern shadow an already-captured /pub link
            if url not in found:
                k = "drive_file" if kind == "drive_open" else kind
                found[url] = {"kind": k, "file_id": file_id, "url": url}
    return list(found.values())


# ---------------------------------------------------------------------------
# Service-account auth (optional)
# ---------------------------------------------------------------------------

def _service_account_token(db) -> str | None:
    """Mint a Drive read-only access token from a configured service-account key.
    Returns None if not configured or google-auth isn't installed."""
    try:
        from .settings_service import get_setting  # generic getter if present
    except Exception:
        get_setting = None

    import os
    key_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not key_json and get_setting:
        try:
            key_json = get_setting(db, "google_service_account_json")
        except Exception:
            key_json = None
    if not key_json:
        return None
    try:
        from google.oauth2 import service_account  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
        info = json.loads(key_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        creds.refresh(Request())
        return creds.token
    except Exception as exc:
        log.warning("gdrive: service-account auth unavailable (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", s)).strip()


def fetch_document(link: dict, db) -> tuple[str | None, str, str | None]:
    """
    Fetch a single Google link. Returns (text, status, file_type).
    status in: fetched | no_access | unsupported | failed
    Tries the public/export endpoints first; uses a service-account bearer token
    when one is configured (for private files).
    """
    kind = link["kind"]
    fid = link["file_id"]
    token = _service_account_token(db)
    auth_headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        if kind == "folder":
            return None, "unsupported", None  # folder listing needs the Drive API (future)

        if kind == "pub":
            r = httpx.get(link["url"], follow_redirects=True, timeout=_TIMEOUT)
            if r.status_code == 200 and r.text:
                return _html_to_text(r.text)[:_MAX_TEXT], "fetched", "html"
            return None, "no_access", None

        # Authenticated Drive API path (handles private files) ------------------
        if token:
            if kind in ("doc", "slides", "sheet"):
                mime = {"doc": "text/plain", "slides": "text/plain",
                        "sheet": "text/csv"}[kind]
                r = httpx.get(
                    f"https://www.googleapis.com/drive/v3/files/{fid}/export",
                    params={"mimeType": mime}, headers=auth_headers,
                    follow_redirects=True, timeout=_TIMEOUT,
                )
                if r.status_code == 200:
                    return r.text[:_MAX_TEXT], "fetched", "txt"
            else:  # drive_file
                r = httpx.get(
                    f"https://www.googleapis.com/drive/v3/files/{fid}",
                    params={"alt": "media"}, headers=auth_headers,
                    follow_redirects=True, timeout=_TIMEOUT,
                )
                if r.status_code == 200:
                    return _extract_bytes(r.content, link), "fetched", "bin"
            if r.status_code in (401, 403, 404):
                return None, "no_access", None
            return None, "failed", None

        # Public path (no auth) -------------------------------------------------
        if kind == "doc":
            url = f"https://docs.google.com/document/d/{fid}/export?format=txt"
            r = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
        elif kind == "slides":
            url = f"https://docs.google.com/presentation/d/{fid}/export/txt"
            r = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
        elif kind == "sheet":
            url = f"https://docs.google.com/spreadsheets/d/{fid}/export?format=csv"
            r = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
        else:  # drive_file
            url = f"https://drive.google.com/uc?export=download&id={fid}"
            r = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
            if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
                return _extract_bytes(r.content, link), "fetched", "bin"
            return None, "no_access", None

        if r.status_code == 200 and r.text and "<html" not in r.text[:200].lower():
            return r.text[:_MAX_TEXT], "fetched", "txt"
        return None, "no_access", None

    except Exception as exc:
        log.warning("gdrive fetch failed for %s: %s", link["url"][:80], exc)
        return None, "failed", None


def _extract_bytes(data: bytes, link: dict) -> str | None:
    """Extract text from downloaded binary file bytes using the existing extractors."""
    from .extraction_service import extract_text, get_extension
    # guess a filename/extension; default to pdf which is most common for decks/memos
    for ext in ("pdf", "docx", "pptx", "xlsx"):
        try:
            txt = extract_text(f"file.{ext}", data)
            if txt and txt.strip():
                return txt[:_MAX_TEXT]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Discovery across the CRM
# ---------------------------------------------------------------------------

def discover_links(db) -> list[dict]:
    """Scan crm_notes + crm_ventures for Google links. Returns deduped link dicts
    enriched with crm_venture_id + source_ref."""
    from ..models import CrmNote, CrmVenture
    from sqlalchemy import select
    from sqlalchemy.orm import undefer

    seen: dict[str, dict] = {}

    notes = db.scalars(
        select(CrmNote).options(undefer(CrmNote.content_text))
    ).all()
    for n in notes:
        for lk in extract_drive_links(n.content_text or ""):
            lk = dict(lk, crm_venture_id=n.crm_venture_id, source_ref=f"note:{n.id}")
            seen.setdefault(lk["url"], lk)

    ventures = db.scalars(
        select(CrmVenture).options(
            undefer(CrmVenture.raw_entry_json), undefer(CrmVenture.raw_record_json)
        )
    ).all()
    for v in ventures:
        blob = (v.raw_entry_json or "") + "\n" + (v.raw_record_json or "")
        for lk in extract_drive_links(blob):
            lk = dict(lk, crm_venture_id=v.id, source_ref=f"venture:{v.id}")
            seen.setdefault(lk["url"], lk)

    return list(seen.values())


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _index_external_doc(doc, db) -> int:
    """Chunk + embed + sanitize a fetched ExternalDocument's text into knowledge_chunks."""
    from ..models import KnowledgeSource, KnowledgeChunk, CrmVenture
    from .embeddings import embed_text, chunk_text
    from .settings_service import get_openrouter_api_key
    from .knowledge_indexer import sanitize_text_llm, infer_basic_themes
    from .vector_store import set_row_vector
    from sqlalchemy import select, delete

    if not doc.raw_text:
        return 0
    api_key = get_openrouter_api_key(db)
    if not api_key:
        return 0

    venture = db.get(CrmVenture, doc.crm_venture_id) if doc.crm_venture_id else None
    company = venture.name if venture else None
    sector = venture.sector if venture else None

    header = []
    if company:
        header.append(f"Company: {company}")
    header.append(f"Document: {doc.title or doc.kind or 'Google document'}")
    header.append("Source: Google Drive")
    prefix = "\n".join(header)

    # Remove any previous chunks for this doc
    old = db.scalar(select(KnowledgeSource).where(
        KnowledgeSource.source_type == "gdrive", KnowledgeSource.source_id == doc.id
    ))
    if old:
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_source_id == old.id))
        db.delete(old)
        db.flush()

    src = KnowledgeSource(
        source_type="gdrive", source_id=doc.id,
        crm_venture_id=doc.crm_venture_id,
        title=doc.title or (f"{company} — Google doc" if company else "Google doc"),
        visibility="admin", approved=True,
    )
    db.add(src)
    db.flush()

    windows = chunk_text(doc.raw_text) or [doc.raw_text]
    stored = 0
    pending_vectors = []
    for w in windows:
        body = f"{prefix}\n\n{w}"
        try:
            vec = embed_text(body, api_key)
        except Exception as exc:
            log.warning("_index_external_doc: embed failed for doc %d: %s", doc.id, exc)
            continue
        themes = infer_basic_themes(body, sector)
        chunk = KnowledgeChunk(
            knowledge_source_id=src.id,
            crm_venture_id=doc.crm_venture_id,
            source_type="gdrive",
            source_id=doc.id,
            text=body,
            sanitized_text=sanitize_text_llm(body, api_key),
            embedding=json.dumps(vec),
            sector=sector,
            themes_json=json.dumps(themes) if themes else None,
            visibility="admin",
            approved=True,
        )
        db.add(chunk)
        db.flush()
        pending_vectors.append((chunk.id, vec))
        stored += 1

    db.commit()
    for cid, vec in pending_vectors:
        try:
            set_row_vector(db, "knowledge_chunks", cid, vec)
        except Exception:
            pass
    return stored


def ingest_external_documents(db) -> dict:
    """
    Discover Google links in the CRM, fetch each one, and index the content.
    Returns a summary dict. Safe to run repeatedly (idempotent per URL).
    """
    from ..models import ExternalDocument, ExternalDocStatus
    from sqlalchemy import select

    links = discover_links(db)
    summary = {"links": len(links), "fetched": 0, "indexed_chunks": 0,
               "no_access": 0, "unsupported": 0, "failed": 0}

    for lk in links:
        doc = db.scalar(select(ExternalDocument).where(ExternalDocument.url == lk["url"]))
        if doc is None:
            doc = ExternalDocument(
                url=lk["url"], provider="gdrive", kind=lk["kind"],
                file_id=lk.get("file_id"), crm_venture_id=lk.get("crm_venture_id"),
                source_ref=lk.get("source_ref"),
            )
            db.add(doc)
            db.commit()
            db.refresh(doc)

        text, status, file_type = fetch_document(lk, db)
        doc.status = ExternalDocStatus(status)
        doc.file_type = file_type
        doc.fetched_at = datetime.utcnow()
        if text:
            doc.raw_text = text
            doc.sha256 = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        db.commit()

        summary[status] = summary.get(status, 0) + (1 if status != "fetched" else 0)
        if status == "fetched" and text:
            summary["fetched"] += 1
            try:
                summary["indexed_chunks"] += _index_external_doc(doc, db)
            except Exception as exc:
                log.error("ingest: indexing failed for doc %d: %s", doc.id, exc)

    log.info("ingest_external_documents: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Drive changes.list() polling  (catches files added directly to Drive)
# ---------------------------------------------------------------------------

_DRIVE_TOKEN_SETTING_KEY = "drive_changes_page_token"

_EXPORTABLE = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet":  "text/csv",
}


def _get_drive_token(db) -> str | None:
    """Load the saved Drive pageToken from app_settings."""
    from ..models import AppSetting
    from sqlalchemy import select
    row = db.scalar(select(AppSetting).where(AppSetting.key == _DRIVE_TOKEN_SETTING_KEY))
    return row.value if row else None


def _save_drive_token(db, token: str) -> None:
    """Persist the latest Drive pageToken into app_settings."""
    from ..models import AppSetting
    from sqlalchemy import select
    row = db.scalar(select(AppSetting).where(AppSetting.key == _DRIVE_TOKEN_SETTING_KEY))
    if row:
        row.value = token
    else:
        db.add(AppSetting(key=_DRIVE_TOKEN_SETTING_KEY, value=token))
    db.commit()


def _get_drive_start_token(token: str) -> str:
    """Call Drive API to get the current startPageToken."""
    resp = httpx.get(
        "https://www.googleapis.com/drive/v3/changes/startPageToken",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("startPageToken", "")


def poll_drive_changes(db) -> dict:
    """
    Fetch all Drive file changes since the last saved pageToken and ingest
    any new or modified files that have not yet been seen (or whose content
    has changed since last fetch).

    Requires GOOGLE_SERVICE_ACCOUNT_JSON to be configured (same as the rest
    of the Drive ingest). Safe to call repeatedly — skips unchanged files via
    the sha256 check on ExternalDocument.

    Returns a summary dict: {changed, ingested, skipped, errors}.
    """
    import os, json as _json
    from sqlalchemy import select as _sel
    from ..models import ExternalDocument, ExternalDocStatus

    # ── Auth ──────────────────────────────────────────────────────────────────
    access_token = _service_account_token(db)
    if not access_token:
        log.info("poll_drive_changes: no service-account configured — skipping Drive poll")
        return {"changed": 0, "ingested": 0, "skipped": 0, "errors": 0}

    auth = {"Authorization": f"Bearer {access_token}"}

    # ── pageToken ──────────────────────────────────────────────────────────────
    page_token = _get_drive_token(db)
    if not page_token:
        # First run — save current token and exit; pick up changes next time
        try:
            start = _get_drive_start_token(access_token)
            _save_drive_token(db, start)
            log.info("poll_drive_changes: first run — saved start token, will track from next poll")
        except Exception as exc:
            log.warning("poll_drive_changes: could not get start token: %s", exc)
        return {"changed": 0, "ingested": 0, "skipped": 0, "errors": 0}

    summary = {"changed": 0, "ingested": 0, "skipped": 0, "errors": 0}
    new_token = page_token

    # ── Iterate change pages ───────────────────────────────────────────────────
    while page_token:
        try:
            resp = httpx.get(
                "https://www.googleapis.com/drive/v3/changes",
                headers=auth,
                params={
                    "pageToken": page_token,
                    "fields": "nextPageToken,newStartPageToken,"
                              "changes(fileId,removed,file(id,name,mimeType,webViewLink,trashed))",
                    "spaces": "drive",
                    "includeRemoved": "true",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("poll_drive_changes: changes.list failed: %s", exc)
            summary["errors"] += 1
            break

        for change in data.get("changes", []):
            file_id = change.get("fileId")
            removed = change.get("removed", False)
            file_meta = change.get("file") or {}
            trashed = file_meta.get("trashed", False)
            mime = file_meta.get("mimeType", "")
            name = file_meta.get("name", "")
            view_url = file_meta.get("webViewLink", "")

            summary["changed"] += 1

            # Soft-delete knowledge chunks for removed/trashed files
            if removed or trashed:
                existing = db.scalar(
                    _sel(ExternalDocument).where(ExternalDocument.file_id == file_id)
                )
                if existing:
                    from ..models import KnowledgeChunk, KnowledgeSource
                    from sqlalchemy import delete as _del
                    old_src = db.scalar(
                        _sel(KnowledgeSource).where(
                            KnowledgeSource.source_type == "gdrive",
                            KnowledgeSource.source_id == existing.id,
                        )
                    )
                    if old_src:
                        db.execute(_del(KnowledgeChunk).where(
                            KnowledgeChunk.knowledge_source_id == old_src.id
                        ))
                        db.delete(old_src)
                    existing.status = ExternalDocStatus.failed
                    db.commit()
                continue

            # Only process exportable / text types
            if mime not in _EXPORTABLE and not mime.startswith("text/"):
                summary["skipped"] += 1
                continue

            # Build a synthetic link dict compatible with fetch_document
            kind = "doc" if "document" in mime else \
                   "slides" if "presentation" in mime else \
                   "sheet" if "spreadsheet" in mime else "drive_file"
            lk = {"kind": kind, "file_id": file_id, "url": view_url or f"https://drive.google.com/file/d/{file_id}"}

            # Fetch content
            text, status, file_type = fetch_document(lk, db)
            if status != "fetched" or not text:
                summary["skipped"] += 1
                continue

            new_sha = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()

            # Upsert ExternalDocument
            doc = db.scalar(_sel(ExternalDocument).where(ExternalDocument.file_id == file_id))
            if doc and doc.sha256 == new_sha:
                # Content unchanged — skip re-embedding
                summary["skipped"] += 1
                continue

            if doc is None:
                doc = ExternalDocument(
                    url=lk["url"], provider="gdrive", kind=kind,
                    file_id=file_id, title=name,
                )
                db.add(doc)
                db.commit()
                db.refresh(doc)

            doc.raw_text = text
            doc.sha256 = new_sha
            doc.status = ExternalDocStatus.fetched
            doc.file_type = file_type
            doc.fetched_at = datetime.utcnow()
            db.commit()

            try:
                _index_external_doc(doc, db)
                summary["ingested"] += 1
            except Exception as exc:
                log.error("poll_drive_changes: indexing failed for %s: %s", file_id, exc)
                summary["errors"] += 1

        new_token = data.get("newStartPageToken") or data.get("nextPageToken", new_token)
        page_token = data.get("nextPageToken")  # None → we've consumed all pages

    _save_drive_token(db, new_token)
    log.info("poll_drive_changes: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Company bucket folder sync (admin portal)
# ---------------------------------------------------------------------------

_FOLDER_ID_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)", re.I)

# Filename patterns that indicate a quarterly reporting period
_PERIOD_PATTERNS = [
    re.compile(r"Q([1-4])[\s._'-]*(?:20)?(\d{2})", re.I),        # Q1 2026 / Q1'26 / Q1-26
    re.compile(r"(?:20)(\d{2})[\s._'-]*Q([1-4])", re.I),         # 2026 Q1 / 2026-Q1
]

_SYNC_EXTENSIONS = {"pdf", "docx", "xlsx", "pptx", "txt", "md", "csv"}


def _guess_quarter_period(filename: str) -> tuple[int, int] | None:
    """Return (year, quarter) if the filename looks like a quarterly report."""
    m = _PERIOD_PATTERNS[0].search(filename)
    if m:
        return 2000 + int(m.group(2)), int(m.group(1))
    m = _PERIOD_PATTERNS[1].search(filename)
    if m:
        return 2000 + int(m.group(1)), int(m.group(2))
    return None


def sync_company_drive_folder(company, db, uploaded_by_id: int) -> dict:
    """
    Pull new files from the company's linked Drive folder into its document bucket.

    - Lists files in the folder via the Drive API (service account required).
    - Skips files already present (same sha256 for this company).
    - Extracts text through the normal pipeline; keeps the original bytes.
    - Quarterly period is guessed from the filename (e.g. "Acme Q1 2026.pdf").

    Returns {"status": ..., "added": n, "skipped": n, "failed": n, "message": str}.
    """
    from sqlalchemy import select as _select
    from ..models import (
        Document, DocumentCategory, ExtractionStatus, ReviewStatus, UploadStatus,
    )
    from .extraction_service import extract_text

    if not company.drive_folder_url:
        return {"status": "error", "message": "No Drive folder linked."}

    m = _FOLDER_ID_RE.search(company.drive_folder_url)
    if not m:
        return {"status": "error", "message": "Could not parse the Drive folder URL."}
    folder_id = m.group(1)

    token = _service_account_token(db)
    if not token:
        return {
            "status": "error",
            "message": (
                "Google service account is not configured. Set "
                "GOOGLE_SERVICE_ACCOUNT_JSON and share the folder with the "
                "service-account email."
            ),
        }
    headers = {"Authorization": f"Bearer {token}"}

    # List folder contents (paged)
    files: list[dict] = []
    page_token = None
    try:
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size)",
                "pageSize": 100,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            r = httpx.get(
                "https://www.googleapis.com/drive/v3/files",
                params=params, headers=headers, timeout=_TIMEOUT,
            )
            if r.status_code in (401, 403, 404):
                return {"status": "error",
                        "message": "No access to the folder — share it with the service-account email."}
            r.raise_for_status()
            data = r.json()
            files.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        log.warning("drive folder sync: listing failed (%s)", exc)
        return {"status": "error", "message": f"Drive listing failed: {exc}"}

    # Existing hashes for dedupe
    existing_hashes = set(
        db.scalars(
            _select(Document.sha256).where(
                Document.company_id == company.id, Document.sha256.is_not(None)
            )
        ).all()
    )

    added = skipped = failed = 0
    for f in files:
        name = f.get("name", "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _SYNC_EXTENSIONS:
            skipped += 1
            continue
        try:
            r = httpx.get(
                f"https://www.googleapis.com/drive/v3/files/{f['id']}",
                params={"alt": "media", "supportsAllDrives": "true"},
                headers=headers, follow_redirects=True, timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                failed += 1
                continue
            data = r.content
            digest = hashlib.sha256(data).hexdigest()
            if digest in existing_hashes:
                skipped += 1
                continue

            raw_text = None
            extraction_status = ExtractionStatus.complete
            extraction_error = None
            try:
                raw_text = extract_text(name, data)
            except Exception as exc:
                extraction_status = ExtractionStatus.failed
                extraction_error = str(exc)
            if raw_text is not None and len(raw_text.strip()) < 20:
                extraction_status = ExtractionStatus.failed
                extraction_error = "No usable text extracted."

            period = _guess_quarter_period(name)
            doc = Document(
                company_id=company.id,
                uploaded_by_id=uploaded_by_id,
                title=name.rsplit(".", 1)[0],
                filename=name[:256],
                file_type=ext,
                file_size=len(data),
                sha256=digest,
                file_bytes=data,
                raw_text=raw_text,
                upload_status=UploadStatus.uploaded,
                extraction_status=extraction_status,
                extraction_error=extraction_error,
                review_status=ReviewStatus.pending,
                document_category=(
                    DocumentCategory.quarterly_reporting if period
                    else DocumentCategory.other
                ),
                is_regular_reporting=bool(period),
                reporting_period=f"{period[0]}-Q{period[1]}" if period else None,
                reporting_year=period[0] if period else None,
                reporting_quarter=period[1] if period else None,
            )
            db.add(doc)
            db.commit()
            existing_hashes.add(digest)
            added += 1

            # Same post-upload pipeline as manual uploads (best-effort):
            # LLM KPI extraction + full-text embedding for portfolio chat.
            if extraction_status == ExtractionStatus.complete and raw_text:
                try:
                    from .portfolio_extraction import run_portfolio_extraction
                    run_portfolio_extraction(doc.id, db)
                except Exception as exc:
                    log.warning("drive sync: extraction failed for doc %d (%s)", doc.id, exc)
                try:
                    from .document_indexer import index_document
                    index_document(doc.id, db)
                except Exception as exc:
                    log.warning("drive sync: raw-text embedding failed for doc %d (%s)", doc.id, exc)
        except Exception as exc:
            db.rollback()
            log.warning("drive folder sync: %s failed (%s)", name[:60], exc)
            failed += 1

    return {
        "status": "ok",
        "added": added, "skipped": skipped, "failed": failed,
        "message": f"Sync complete: {added} added, {skipped} skipped, {failed} failed.",
    }
