"""
Attio sync services.

Entry points:
  sync_attio_list_ventures(db, limit=None)   ← list-entry sync (primary)
  sync_attio_ventures(db, limit=None)        ← object-record sync (legacy fallback)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CrmSyncRun, CrmSyncStatus, CrmVenture

log = logging.getLogger(__name__)

_UPSERT_FIELDS = (
    "name", "website", "description", "stage", "sector",
    "owner", "source", "status", "attio_url",
)

# How often to flush progress to DB during the upsert loop (every N records)
_PROGRESS_FLUSH_EVERY = 10


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def resume_run(run_id: int, limit: int | None = None) -> None:
    """
    Execute a sync run that was already created (used by background thread).
    Opens its own DB session so the HTTP request thread is not blocked.
    """
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        run = db.get(CrmSyncRun, run_id)
        if run is None:
            return
        list_id = _get_list_id(db)
        if list_id:
            _run_list_sync(run, db, limit)
        else:
            _run_object_sync(run, db, limit)

        # Auto-ingest Google Drive/Docs linked in the freshly-synced records
        # (pitch_deck, internal_docs, working_doc, …). Non-fatal: never blocks sync.
        try:
            from .gdrive_ingest import ingest_external_documents
            ingest_external_documents(db)
        except Exception as exc:
            log.warning("post-sync Google Drive ingest skipped: %s", exc)
    except Exception as exc:
        log.error("Background sync error for run %d: %s", run_id, exc, exc_info=True)
        try:
            run = db.get(CrmSyncRun, run_id)
            if run and run.status == CrmSyncStatus.running:
                _fail_run(run, str(exc)[:2000], db)
        except Exception:
            pass
    finally:
        db.close()


def _get_list_id(db: Session) -> str | None:
    from .settings_service import get_attio_list_id_or_slug
    return get_attio_list_id_or_slug(db)


def _start_run(db: Session, sync_type: str) -> CrmSyncRun:
    run = CrmSyncRun(
        sync_type=sync_type,
        status=CrmSyncStatus.running,
        started_at=datetime.utcnow(),
        records_seen=0,
        records_created=0,
        records_updated=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    log.info("CRM sync run %d (%s) started", run.id, sync_type)
    return run


def _fail_run(run: CrmSyncRun, error: str, db: Session) -> CrmSyncRun:
    run.status = CrmSyncStatus.failed
    run.finished_at = datetime.utcnow()
    run.error = error[:2000]
    db.commit()
    log.error("CRM sync run %d failed: %s", run.id, error)
    return run


def _finish_run(
    run: CrmSyncRun, seen: int, created: int, updated: int, db: Session
) -> CrmSyncRun:
    run.status = CrmSyncStatus.success
    run.finished_at = datetime.utcnow()
    run.records_seen = seen
    run.records_created = created
    run.records_updated = updated
    db.commit()
    log.info(
        "CRM sync run %d done: %d seen, %d created, %d updated",
        run.id, seen, created, updated,
    )
    return run


def _apply_fields(venture: CrmVenture, normalized: dict) -> None:
    for f in _UPSERT_FIELDS:
        setattr(venture, f, normalized.get(f))


# ---------------------------------------------------------------------------
# List-entry sync  (primary)
# ---------------------------------------------------------------------------

def _run_list_sync(run: CrmSyncRun, db: Session, limit: int | None = None) -> CrmSyncRun:
    """Internal: execute the list-entry sync against an existing run row."""
    from .attio_client import (
        get_attio_api_key,
        get_attio_records_map,
        normalize_attio_list_entry,
        query_attio_list_entries,
    )
    from .settings_service import get_attio_list_id_or_slug, get_attio_object_slug

    api_key = get_attio_api_key(db)
    if not api_key:
        return _fail_run(run, "Attio API key not configured. Go to Admin → Settings.", db)

    list_id = get_attio_list_id_or_slug(db)
    if not list_id:
        return _fail_run(run, "No Attio List configured. Set it in Admin → Settings.", db)

    object_slug = get_attio_object_slug(db)

    # ── Phase 1: All API calls — no DB connection held during network I/O ────
    try:
        raw_entries = query_attio_list_entries(list_id, api_key, limit=limit)
    except Exception as exc:
        return _fail_run(run, f"Failed to fetch list entries: {exc}", db)

    record_ids = []
    for e in raw_entries:
        parent_rid = e.get("parent_record_id")
        rid = (
            parent_rid.get("record_id") if isinstance(parent_rid, dict)
            else parent_rid if isinstance(parent_rid, str) else None
        )
        if rid:
            record_ids.append(rid)

    try:
        records_map = get_attio_records_map(object_slug, record_ids, api_key)
    except Exception as exc:
        log.warning("Bulk record fetch failed, continuing without record data: %s", exc)
        records_map = {}

    # ── Phase 2: DB writes — no API calls ────────────────────────────────────
    run.records_total = len(raw_entries)
    db.commit()

    created = updated = seen = 0
    now = datetime.utcnow()

    for raw_entry in raw_entries:
        try:
            parent_rid = raw_entry.get("parent_record_id")
            record_id_str = (
                parent_rid.get("record_id") if isinstance(parent_rid, dict)
                else parent_rid if isinstance(parent_rid, str) else None
            )
            record = records_map.get(str(record_id_str)) if record_id_str else None
            normalized = normalize_attio_list_entry(raw_entry, record)
        except Exception as exc:
            entry_id_obj = raw_entry.get("id") or {}
            eid = entry_id_obj.get("entry_id") if isinstance(entry_id_obj, dict) else "?"
            log.warning("Failed to normalize entry %s: %s", eid, exc)
            seen += 1
            continue

        entry_id = normalized.get("attio_entry_id")
        if not entry_id:
            seen += 1
            continue

        raw_entry_json = json.dumps(raw_entry, ensure_ascii=False, default=str)
        raw_record_json = (
            json.dumps(record, ensure_ascii=False, default=str) if record else None
        )

        existing = db.scalar(select(CrmVenture).where(CrmVenture.attio_entry_id == entry_id))
        if existing:
            _apply_fields(existing, normalized)
            existing.attio_record_id = normalized.get("attio_record_id")
            existing.attio_list_id = normalized.get("attio_list_id")
            existing.raw_entry_json = raw_entry_json
            existing.raw_record_json = raw_record_json
            existing.synced_at = now
            updated += 1
        else:
            venture = CrmVenture(
                attio_entry_id=entry_id,
                attio_list_id=normalized.get("attio_list_id"),
                attio_record_id=normalized.get("attio_record_id"),
                raw_entry_json=raw_entry_json,
                raw_record_json=raw_record_json,
                synced_at=now,
            )
            _apply_fields(venture, normalized)
            db.add(venture)
            db.flush()
            created += 1

        seen += 1
        if seen % _PROGRESS_FLUSH_EVERY == 0:
            run.records_seen = seen
            run.records_created = created
            run.records_updated = updated
            db.commit()

    db.flush()
    db.commit()
    return _finish_run(run, seen, created, updated, db)


def _run_object_sync(run: CrmSyncRun, db: Session, limit: int | None = None) -> CrmSyncRun:
    """Internal: execute the object-record sync against an existing run row."""
    from .attio_client import (
        get_attio_api_key,
        normalize_attio_record,
        query_attio_company_records,
    )
    from .settings_service import get_attio_object_slug

    api_key = get_attio_api_key(db)
    if not api_key:
        return _fail_run(run, "Attio API key not configured. Go to Admin → Settings.", db)

    object_slug = get_attio_object_slug(db)

    try:
        raw_records = query_attio_company_records(object_slug, api_key, limit=limit)
    except Exception as exc:
        return _fail_run(run, f"Failed to fetch records from Attio: {exc}", db)

    run.records_total = len(raw_records)
    db.commit()

    created = updated = seen = 0
    now = datetime.utcnow()

    for raw in raw_records:
        try:
            normalized = normalize_attio_record(raw)
        except Exception as exc:
            log.warning("Failed to normalize record %s: %s", raw.get("id"), exc)
            seen += 1
            continue

        record_id = normalized.get("attio_record_id")
        if not record_id:
            seen += 1
            continue

        raw_json = json.dumps(raw, ensure_ascii=False, default=str)
        existing = db.scalar(select(CrmVenture).where(CrmVenture.attio_record_id == record_id))
        if existing:
            _apply_fields(existing, normalized)
            existing.raw_attio_json = raw_json
            existing.synced_at = now
            updated += 1
        else:
            venture = CrmVenture(attio_record_id=record_id, raw_attio_json=raw_json, synced_at=now)
            _apply_fields(venture, normalized)
            db.add(venture)
            created += 1

        seen += 1
        if seen % _PROGRESS_FLUSH_EVERY == 0:
            run.records_seen = seen
            run.records_created = created
            run.records_updated = updated
            db.commit()

    db.flush()
    db.commit()
    return _finish_run(run, seen, created, updated, db)


def sync_attio_list_ventures(db: Session, limit: int | None = None) -> CrmSyncRun:
    run = _start_run(db, "attio_list")
    return _run_list_sync(run, db, limit)


# ---------------------------------------------------------------------------
# Single-record / single-note targeted sync  (used by webhook handler)
# ---------------------------------------------------------------------------

def sync_single_note(attio_note_id: str, db: Session) -> int | None:
    """
    Fetch one Attio note by its note_id, upsert it into crm_notes, and
    return the local crm_note.id (or None on failure).

    Used by the webhook handler for immediate note.created / note.updated events.
    """
    from .attio_client import get_attio_api_key, get_attio_note_body, normalize_attio_note
    from ..models import CrmNote, CrmVenture

    api_key = get_attio_api_key(db)
    if not api_key:
        log.warning("sync_single_note: no API key configured")
        return None

    import httpx
    try:
        resp = httpx.get(
            f"https://api.attio.com/v2/notes/{attio_note_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        raw_note = resp.json().get("data", {})
    except Exception as exc:
        log.error("sync_single_note: API fetch failed for %s: %s", attio_note_id, exc)
        return None

    # Body may be inline or need a separate fetch
    inline = (raw_note.get("content_plaintext") or "").strip()
    content_text = inline if inline else get_attio_note_body(attio_note_id, api_key)

    try:
        normalized = normalize_attio_note(raw_note, content_text)
    except Exception as exc:
        log.error("sync_single_note: normalization failed: %s", exc)
        return None

    note_id = normalized.get("attio_note_id")
    if not note_id:
        return None

    # Resolve crm_venture_id from the note's parent record_id
    attio_record_id = normalized.get("attio_record_id")
    crm_venture_id: int | None = None
    if attio_record_id:
        venture = db.scalar(
            select(CrmVenture).where(CrmVenture.attio_record_id == attio_record_id)
        )
        crm_venture_id = venture.id if venture else None

    import json as _json
    raw_json = _json.dumps(raw_note, ensure_ascii=False, default=str)
    now = datetime.utcnow()

    existing = db.scalar(select(CrmNote).where(CrmNote.attio_note_id == note_id))
    if existing:
        existing.crm_venture_id = crm_venture_id
        existing.attio_record_id = attio_record_id
        existing.title = normalized.get("title")
        existing.content_text = normalized.get("content_text")
        existing.created_by = normalized.get("created_by")
        existing.created_at_attio = normalized.get("created_at_attio")
        existing.raw_note_json = raw_json
        existing.synced_at = now
        db.commit()
        return existing.id
    else:
        note = CrmNote(
            attio_note_id=note_id,
            crm_venture_id=crm_venture_id,
            attio_record_id=attio_record_id,
            title=normalized.get("title"),
            content_text=normalized.get("content_text"),
            created_by=normalized.get("created_by"),
            created_at_attio=normalized.get("created_at_attio"),
            raw_note_json=raw_json,
            synced_at=now,
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        return note.id


def sync_single_venture(attio_record_id: str, object_slug: str, db: Session) -> int | None:
    """
    Fetch one Attio record by its record_id, upsert it into crm_ventures, and
    return the local crm_venture.id (or None on failure).

    Used by the webhook handler for immediate record.created / record.updated events.
    """
    from .attio_client import get_attio_api_key, get_attio_records_map, normalize_attio_list_entry
    from .settings_service import get_attio_object_slug

    api_key = get_attio_api_key(db)
    if not api_key:
        log.warning("sync_single_venture: no API key configured")
        return None

    slug = object_slug or get_attio_object_slug(db)

    import httpx, json as _json
    try:
        resp = httpx.get(
            f"https://api.attio.com/v2/objects/{slug}/records/{attio_record_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        record = resp.json().get("data", {})
    except Exception as exc:
        log.error("sync_single_venture: API fetch failed for %s: %s", attio_record_id, exc)
        return None

    try:
        from .attio_client import normalize_attio_record
        normalized = normalize_attio_record(record)
    except Exception as exc:
        log.error("sync_single_venture: normalization failed: %s", exc)
        return None

    record_id_str = normalized.get("attio_record_id")
    if not record_id_str:
        return None

    raw_json = _json.dumps(record, ensure_ascii=False, default=str)
    now = datetime.utcnow()

    existing = db.scalar(select(CrmVenture).where(CrmVenture.attio_record_id == record_id_str))
    if existing:
        _apply_fields(existing, normalized)
        existing.raw_record_json = raw_json
        existing.synced_at = now
        db.commit()
        return existing.id
    else:
        venture = CrmVenture(
            attio_record_id=record_id_str,
            raw_record_json=raw_json,
            synced_at=now,
        )
        _apply_fields(venture, normalized)
        db.add(venture)
        db.commit()
        db.refresh(venture)
        return venture.id


# ---------------------------------------------------------------------------
# Object-record sync  (legacy fallback when no list is configured)
# ---------------------------------------------------------------------------

def sync_attio_ventures(db: Session, limit: int | None = None) -> CrmSyncRun:
    run = _start_run(db, "attio_object")
    return _run_object_sync(run, db, limit)


# ---------------------------------------------------------------------------
# Notes sync
# ---------------------------------------------------------------------------

def sync_attio_notes_for_ventures(db: Session, run: "CrmSyncRun | None" = None) -> dict:
    """
    For each crm_venture with an attio_record_id, fetch linked Attio notes
    and upsert into crm_notes.

    Phase 1: all API calls (no DB connection held).
    Phase 2: upsert loop (no API calls).

    Returns {"seen": N, "created": M, "updated": K, "errors": E}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .attio_client import (
        get_attio_api_key,
        get_attio_note_body,
        normalize_attio_note,
        query_attio_notes_for_record,
    )
    from .settings_service import get_attio_object_slug
    from ..models import CrmNote

    api_key = get_attio_api_key(db)
    if not api_key:
        raise RuntimeError("Attio API key not configured.")

    object_slug = get_attio_object_slug(db)

    # ── Phase 1: parallel per-venture note fetches ────────────────────────────
    # Attio does not support GET /v2/notes without a parent_record_id filter (returns 400).
    # We parallelise per-venture calls instead (20 workers → ~20x faster than serial).
    ventures = list(
        db.scalars(
            select(CrmVenture).where(CrmVenture.attio_record_id.is_not(None))
        ).all()
    )

    if run is not None:
        run.records_total = len(ventures)
        db.commit()

    log.info("Fetching notes for %d ventures in parallel (20 workers)", len(ventures))

    def _fetch_venture_notes(venture_tuple):
        vid, rid = venture_tuple
        notes = query_attio_notes_for_record(rid, object_slug, api_key)
        return [(n, vid) for n in notes]

    raw_notes: list[tuple[dict, int]] = []
    errors = 0

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(_fetch_venture_notes, (v.id, v.attio_record_id)): v.id
            for v in ventures
        }
        for fut in as_completed(futures):
            try:
                raw_notes.extend(fut.result())
            except Exception as exc:
                log.warning("Notes fetch failed for venture %s: %s", futures[fut], exc)
                errors += 1

    log.info("Notes fetch complete: %d notes across %d ventures", len(raw_notes), len(ventures))

    # Separate notes with inline content from those needing a body fetch
    need_body: list[tuple[dict, int, str]] = []
    enriched: list[tuple[dict, str | None, int]] = []

    for raw_note, venture_id in raw_notes:
        inline = (raw_note.get("content_plaintext") or "").strip()
        if inline:
            enriched.append((raw_note, inline, venture_id))
            continue
        note_id_obj = raw_note.get("id") or {}
        note_id = (
            note_id_obj.get("note_id")
            if isinstance(note_id_obj, dict)
            else str(note_id_obj) if note_id_obj else None
        )
        if note_id:
            need_body.append((raw_note, venture_id, note_id))
        else:
            enriched.append((raw_note, None, venture_id))

    # Parallel body fetches
    if need_body:
        log.info("Fetching %d note bodies in parallel (20 workers)", len(need_body))

        def _fetch_body(item):
            raw_note, venture_id, note_id = item
            body = get_attio_note_body(note_id, api_key)
            return raw_note, body, venture_id

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_fetch_body, item): item for item in need_body}
            for fut in as_completed(futures):
                try:
                    enriched.append(fut.result())
                except Exception as exc:
                    raw_note, venture_id, note_id = futures[fut]
                    log.warning("Note body fetch failed for %s: %s", note_id, exc)
                    enriched.append((raw_note, None, venture_id))
                    errors += 1

    # ── Phase 2: upsert ───────────────────────────────────────────────────────
    import json
    from datetime import datetime as _dt

    # Deduplicate by attio_note_id — same note can appear via multiple ventures
    seen_note_ids: set[str] = set()
    deduped: list[tuple] = []
    for item in enriched:
        raw_note, content_text, venture_id = item
        nid_obj = raw_note.get("id") or {}
        nid = nid_obj.get("note_id") if isinstance(nid_obj, dict) else str(nid_obj) if nid_obj else None
        if nid and nid in seen_note_ids:
            continue
        if nid:
            seen_note_ids.add(nid)
        deduped.append(item)
    enriched = deduped

    now = _dt.utcnow()
    created = updated = 0

    for raw_note, content_text, venture_id in enriched:
        try:
            normalized = normalize_attio_note(raw_note, content_text)
        except Exception as exc:
            log.warning("Failed to normalize note: %s", exc)
            errors += 1
            continue

        note_id = normalized.get("attio_note_id")
        if not note_id:
            continue

        raw_json = json.dumps(raw_note, ensure_ascii=False, default=str)

        existing = db.scalar(
            select(CrmNote).where(CrmNote.attio_note_id == note_id)
        )
        if existing:
            existing.crm_venture_id = venture_id
            existing.attio_record_id = normalized.get("attio_record_id")
            existing.title = normalized.get("title")
            existing.content_text = normalized.get("content_text")
            existing.created_by = normalized.get("created_by")
            existing.created_at_attio = normalized.get("created_at_attio")
            existing.raw_note_json = raw_json
            existing.synced_at = now
            updated += 1
        else:
            note = CrmNote(
                attio_note_id=note_id,
                crm_venture_id=venture_id,
                attio_record_id=normalized.get("attio_record_id"),
                title=normalized.get("title"),
                content_text=normalized.get("content_text"),
                created_by=normalized.get("created_by"),
                created_at_attio=normalized.get("created_at_attio"),
                raw_note_json=raw_json,
                synced_at=now,
            )
            db.add(note)
            created += 1

    db.flush()
    db.commit()

    log.info(
        "Notes sync done: %d seen, %d created, %d updated, %d errors",
        len(enriched), created, updated, errors,
    )
    result = {
        "seen": len(enriched),
        "created": created,
        "updated": updated,
        "errors": errors,
    }
    if run is not None:
        if errors and not created and not updated:
            _fail_run(run, f"{errors} errors, 0 successful", db)
        else:
            _finish_run(run, result["seen"], result["created"], result["updated"], db)
    return result


def sync_attio_files_for_ventures(db, run: "CrmSyncRun | None" = None) -> dict:
    """
    Two-phase file sync.
    Phase 1: extract file metadata from stored JSON blobs (no extra API calls),
             then download new/changed files and extract their text.
    Phase 2: upsert CrmFile rows.
    """
    import hashlib, json
    from datetime import datetime as _dt
    from sqlalchemy import select as _sel

    from ..models import CrmFile, CrmFileStatus
    from .attio_client import (
        query_attio_files_from_raw,
        download_attio_file,
        normalize_attio_file,
    )
    from .extraction_service import extract_text
    from .attio_client import get_attio_api_key

    api_key = get_attio_api_key(db)

    # ── Phase 1: discover + download ─────────────────────────────────────────
    from sqlalchemy.orm import undefer as _undefer

    ventures = list(db.scalars(
        _sel(CrmVenture)
        .where(CrmVenture.attio_record_id.isnot(None))
        .options(_undefer(CrmVenture.raw_record_json), _undefer(CrmVenture.raw_entry_json))
    ).all())

    if run is not None:
        run.records_total = len(ventures)
        db.commit()

    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    SUPPORTED = {"pdf", "docx", "pptx", "xlsx", "txt"}

    # (normalized_dict, raw_bytes_or_None, content_text_or_None, crm_venture_id)
    to_upsert: list[tuple[dict, bytes | None, str | None, int]] = []
    errors = 0

    # Build a lookup of existing files by attio_file_id → sha256
    existing_sha: dict[str, str] = {}
    for f in db.scalars(_sel(CrmFile)).all():
        if f.attio_file_id and f.sha256:
            existing_sha[f.attio_file_id] = f.sha256

    # Step 1: discover all files from stored JSON (no API calls)
    has_blobs = sum(1 for v in ventures if v.raw_record_json or v.raw_entry_json)
    log.info("Files sync: %d ventures, %d have raw JSON blobs", len(ventures), has_blobs)

    discovered: list[tuple[dict, int]] = []  # (normalized, venture_id)
    for venture in ventures:
        try:
            raw_files = query_attio_files_from_raw(
                venture.raw_record_json, venture.raw_entry_json
            )
        except Exception as exc:
            log.warning(
                "Failed to extract files from venture %d (%s): %s",
                venture.id, venture.attio_record_id, exc,
            )
            errors += 1
            continue

        for raw_file in raw_files:
            normalized = normalize_attio_file(raw_file)
            if normalized.get("attio_file_id"):
                discovered.append((normalized, venture.id))

    # Separate unsupported (no download needed) from ones to download
    need_download: list[tuple[dict, int]] = []
    for normalized, venture_id in discovered:
        file_type = (normalized.get("file_type") or "").lower()
        if file_type not in SUPPORTED:
            to_upsert.append((normalized, None, None, venture_id))
        else:
            need_download.append((normalized, venture_id))

    log.info(
        "Files discovery: %d total, %d to download, %d unsupported",
        len(discovered), len(need_download), len(discovered) - len(need_download),
    )

    # Step 2: parallel downloads + text extraction (up to 8 workers)
    def _process_file(item: tuple[dict, int]):
        normalized, venture_id = item
        file_id = normalized["attio_file_id"]
        file_type = (normalized.get("file_type") or "").lower()

        # Skip download if sha256 unchanged
        data = download_attio_file(file_id, normalized.get("download_url"), api_key)
        if data is None:
            return normalized, None, None, venture_id

        sha = hashlib.sha256(data).hexdigest()
        if existing_sha.get(file_id) == sha:
            return normalized, data, "__unchanged__", venture_id

        try:
            content_text = extract_text(
                normalized.get("filename") or f"file.{file_type}", data
            )
        except Exception as exc:
            log.warning("Text extraction failed for %s: %s", file_id, exc)
            content_text = None

        return normalized, data, content_text, venture_id

    if need_download:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_process_file, item): item for item in need_download}
            for fut in _as_completed(futures):
                try:
                    to_upsert.append(fut.result())
                except Exception as exc:
                    normalized, _ = futures[fut]
                    log.warning("File processing failed for %s: %s", normalized.get("attio_file_id"), exc)
                    to_upsert.append((normalized, None, None, futures[fut][1]))
                    errors += 1

    log.info("Files discovery complete: %d files across %d ventures", len(to_upsert), len(ventures))

    # ── Phase 2: upsert ───────────────────────────────────────────────────────
    import hashlib as _hl

    now = _dt.utcnow()
    created = updated = 0

    for normalized, data, content_text, venture_id in to_upsert:
        import json as _json

        file_id = normalized.get("attio_file_id")
        file_type = (normalized.get("file_type") or "").lower()
        SUPPORTED = {"pdf", "docx", "pptx", "xlsx", "txt"}

        sha = _hl.sha256(data).hexdigest() if data else None

        if content_text == "__unchanged__":
            # Only update metadata, keep existing text
            existing = db.scalar(_sel(CrmFile).where(CrmFile.attio_file_id == file_id))
            if existing:
                existing.crm_venture_id = venture_id
                existing.filename = normalized.get("filename")
                existing.file_type = normalized.get("file_type")
                existing.mime_type = normalized.get("mime_type")
                existing.file_size = normalized.get("file_size")
                existing.download_url = normalized.get("download_url")
                existing.synced_at = now
                updated += 1
            continue

        if file_type not in SUPPORTED:
            status = CrmFileStatus.unsupported
        elif data is None:
            status = CrmFileStatus.failed
        elif content_text:
            status = CrmFileStatus.text_extracted
        else:
            status = CrmFileStatus.failed

        existing = db.scalar(_sel(CrmFile).where(CrmFile.attio_file_id == file_id))
        if existing:
            existing.crm_venture_id = venture_id
            existing.filename = normalized.get("filename")
            existing.file_type = normalized.get("file_type")
            existing.mime_type = normalized.get("mime_type")
            existing.file_size = normalized.get("file_size")
            existing.download_url = normalized.get("download_url")
            existing.sha256 = sha
            existing.raw_text = content_text if content_text else existing.raw_text
            existing.extraction_status = status
            existing.synced_at = now
            updated += 1
        else:
            f = CrmFile(
                attio_file_id=file_id,
                crm_venture_id=venture_id,
                attio_record_id=None,
                filename=normalized.get("filename"),
                file_type=normalized.get("file_type"),
                mime_type=normalized.get("mime_type"),
                file_size=normalized.get("file_size"),
                download_url=normalized.get("download_url"),
                sha256=sha,
                raw_text=content_text,
                extraction_status=status,
                synced_at=now,
            )
            db.add(f)
            created += 1

    db.flush()
    db.commit()

    log.info(
        "Files sync done: %d seen, %d created, %d updated, %d errors",
        len(to_upsert), created, updated, errors,
    )
    result = {
        "seen": len(to_upsert),
        "created": created,
        "updated": updated,
        "errors": errors,
    }
    if run is not None:
        if errors and not created and not updated:
            _fail_run(run, f"{errors} errors, 0 successful", db)
        else:
            _finish_run(run, result["seen"], result["created"], result["updated"], db)
    return result
