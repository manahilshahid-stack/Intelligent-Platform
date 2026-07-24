"""
Attio REST API client.

Public API:
  - get_attio_api_key(db=None)
  - test_attio_connection(api_key)
  - list_attio_object_attributes(object_slug, api_key)
  - query_attio_list_entries(list_id_or_slug, api_key)      ← list sync
  - get_attio_record(object_slug, record_id, api_key)        ← fetch linked record
  - normalize_attio_list_entry(entry, record=None)           ← list sync normalizer
  - query_attio_company_records(object_slug, api_key)        ← object sync (legacy)
  - normalize_attio_record(record)                           ← object sync normalizer

Attio API base: https://api.attio.com/v2
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.attio.com/v2"
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------

def get_attio_api_key(db=None) -> str | None:
    env_key = os.environ.get("ATTIO_API_KEY", "").strip()
    if env_key:
        return env_key
    if db is not None:
        from .settings_service import get_attio_api_key as _db_key
        return _db_key(db)
    return None


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get(path: str, api_key: str, params: dict | None = None) -> dict:
    resp = httpx.get(f"{_BASE}{path}", headers=_headers(api_key), params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, api_key: str, body: dict) -> dict:
    resp = httpx.post(f"{_BASE}{path}", headers=_headers(api_key), json=body, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Value extraction helpers
# ---------------------------------------------------------------------------

def _pick_value(values: list[dict]) -> Any:
    """
    Extract the first meaningful value from an Attio attribute value list.
    Handles: text, number, domain, email, select/status option, actor/person ref.
    """
    if not isinstance(values, list) or not values:
        return None
    for entry in values:
        if not isinstance(entry, dict):
            continue
        # Domain / website
        if "domain" in entry and entry["domain"]:
            return entry["domain"]
        # Email address
        if "email_address" in entry and entry["email_address"]:
            return entry["email_address"]
        # Phone number
        if "phone_number" in entry and entry["phone_number"]:
            return entry["phone_number"]
        # Person / actor name (owner, assigned-to)
        if "first_name" in entry or "last_name" in entry:
            parts = [entry.get("first_name") or "", entry.get("last_name") or ""]
            name = " ".join(p for p in parts if p).strip()
            if name:
                return name
        if "name" in entry and isinstance(entry["name"], str) and entry["name"]:
            return entry["name"]
        # Select / status option object
        if "option" in entry:
            opt = entry["option"]
            if isinstance(opt, dict):
                return opt.get("title") or opt.get("value") or opt.get("api_slug")
            if opt is not None:
                return str(opt)
        # Status value (new Attio schema variant)
        if "status" in entry:
            s = entry["status"]
            if isinstance(s, dict):
                return s.get("title") or s.get("api_slug")
            if s is not None:
                return str(s)
        # Currency
        if "currency_value" in entry and entry["currency_value"] is not None:
            return str(entry["currency_value"])
        # Actor reference — resolved_name preferred
        if "resolved_name" in entry and entry["resolved_name"]:
            return entry["resolved_name"]
        # Plain scalar value (text, number, boolean, date, timestamp)
        v = entry.get("value")
        if v is not None and v != "":
            return v
        # Record reference — return record_id as string
        if "target_record_id" in entry and entry["target_record_id"]:
            return str(entry["target_record_id"])
    return None


def _pick_from(values_dict: dict, slugs: list[str]) -> str | None:
    for slug in slugs:
        v = _pick_value(values_dict.get(slug) or [])
        if v is not None:
            s = str(v).strip()
            if s:
                return s
    return None


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_attio_connection(api_key: str) -> tuple[bool, str]:
    try:
        data = _get("/self", api_key)
        d = data.get("data") or data  # some versions nest under "data", others don't
        log.debug("Attio /self response: %s", data)
        workspace = (
            d.get("workspace_name")
            or d.get("name")
            or (d.get("workspace") or {}).get("name")
            or next((v for v in d.values() if isinstance(v, str) and v), None)
            or "Attio"
        )
        return True, f"Connected to Attio workspace: {workspace}"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return False, "Invalid API key (401 Unauthorized)."
        try:
            detail = exc.response.json().get("message") or exc.response.text[:200]
        except Exception:
            detail = exc.response.text[:200]
        return False, f"Attio returned HTTP {exc.response.status_code}: {detail}"
    except httpx.RequestError as exc:
        return False, f"Network error reaching Attio: {exc}"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Object attributes (for settings UI / field discovery)
# ---------------------------------------------------------------------------

def list_attio_object_attributes(object_slug: str, api_key: str) -> list[dict]:
    data = _get(f"/objects/{object_slug}/attributes", api_key)
    attrs = data.get("data", [])
    return [
        {
            "api_slug": a.get("api_slug"),
            "title": a.get("title"),
            "type": a.get("type"),
            "is_required": a.get("is_required", False),
            "is_unique": a.get("is_unique", False),
        }
        for a in attrs
    ]


# ---------------------------------------------------------------------------
# List entries  (primary sync path)
# ---------------------------------------------------------------------------

def query_attio_list_entries(
    list_id_or_slug: str, api_key: str, limit: int | None = None
) -> list[dict]:
    """
    Page through entries of the given Attio list.
    POST /v2/lists/{list}/entries/query

    limit: if set, stop after collecting this many entries.
    Returns the raw entry dicts. Each entry contains:
      id.entry_id, id.list_id, parent_record_id, parent_object, entry_values
    """
    entries: list[dict] = []
    offset = 0
    page_size = 500

    while True:
        # Don't fetch more than we need on the last page
        fetch = page_size if limit is None else min(page_size, limit - len(entries))
        try:
            data = _post(
                f"/lists/{list_id_or_slug}/entries/query",
                api_key,
                {"limit": fetch, "offset": offset},
            )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"List entries query failed ({exc.response.status_code}): "
                f"{exc.response.text[:400]}"
            ) from exc

        page = data.get("data", [])
        entries.extend(page)
        log.debug("List %r: fetched %d entries (offset %d)", list_id_or_slug, len(page), offset)

        if len(page) < fetch:
            break
        if limit is not None and len(entries) >= limit:
            break
        offset += page_size

    log.info("Fetched %d entries from Attio list %r", len(entries), list_id_or_slug)
    return entries


def get_attio_records_map(
    object_slug: str, record_ids: list[str], api_key: str
) -> dict[str, dict]:
    """
    Fetch multiple company records in bulk (paginated) and return {record_id: record}.
    Stops paging early once all requested IDs are found.
    Replaces N individual get_attio_record() calls with ceil(N/500) paginated queries.
    """
    if not record_ids:
        return {}

    target = {str(r) for r in record_ids if r}
    result: dict[str, dict] = {}
    offset = 0
    page_size = 500

    while target:
        try:
            data = _post(
                f"/objects/{object_slug}/records/query",
                api_key,
                {"limit": page_size, "offset": offset},
            )
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Bulk record fetch failed at offset %d (%d): %s",
                offset, exc.response.status_code, exc.response.text[:200],
            )
            break

        page = data.get("data", [])
        if not page:
            break

        for r in page:
            rid = (r.get("id") or {}).get("record_id")
            if rid and str(rid) in target:
                result[str(rid)] = r
                target.discard(str(rid))

        if len(page) < page_size or not target:
            break
        offset += page_size

    log.info(
        "get_attio_records_map: fetched %d/%d requested records",
        len(result), len(record_ids),
    )
    return result


def get_attio_record(object_slug: str, record_id: str, api_key: str) -> dict | None:
    """
    Fetch a single Attio object record by ID.
    GET /v2/objects/{object_slug}/records/{record_id}
    Returns the record dict (data key unwrapped), or None on 404.
    """
    try:
        data = _get(f"/objects/{object_slug}/records/{record_id}", api_key)
        return data.get("data")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        log.warning(
            "Failed to fetch record %s/%s: HTTP %d",
            object_slug, record_id, exc.response.status_code,
        )
        return None
    except Exception as exc:
        log.warning("Failed to fetch record %s/%s: %s", object_slug, record_id, exc)
        return None


def normalize_attio_list_entry(entry: dict, record: dict | None = None) -> dict:
    """
    Normalise a raw Attio list entry (+ optional linked record) into CrmVenture fields.

    entry_values  → pipeline-specific attrs: stage, owner, status, source
    record.values → company attrs: name, domains/website, description, sector
    Both sides are tried for every field so the mapping is resilient to workspace variation.
    """
    entry_id_obj = entry.get("id") or {}
    entry_id = entry_id_obj.get("entry_id") if isinstance(entry_id_obj, dict) else None
    list_id = entry_id_obj.get("list_id") if isinstance(entry_id_obj, dict) else None

    # Parent record ID (the company linked to this list entry)
    parent_rid = entry.get("parent_record_id")
    if isinstance(parent_rid, dict):
        attio_record_id = parent_rid.get("record_id")
    elif isinstance(parent_rid, str):
        attio_record_id = parent_rid
    else:
        attio_record_id = None

    # If not found in entry, try from the record itself
    if not attio_record_id and record:
        rid = record.get("id") or {}
        if isinstance(rid, dict):
            attio_record_id = rid.get("record_id")

    ev: dict = entry.get("entry_values") or {}
    rv: dict = (record.get("values") if record else None) or {}

    def pick(entry_slugs: list[str], record_slugs: list[str] | None = None) -> str | None:
        # Try entry_values first (pipeline-specific), then record values
        v = _pick_from(ev, entry_slugs)
        if v:
            return v
        v = _pick_from(rv, record_slugs or entry_slugs)
        if v:
            return v
        # Cross-try: entry slugs in record, record slugs in entry
        if record_slugs:
            v = _pick_from(ev, record_slugs)
            if v:
                return v
        return _pick_from(rv, entry_slugs)

    name = pick(["name", "company_name", "title"])
    website = pick(["domains", "website", "domain", "url", "web"])
    description = pick(["description", "about", "bio", "notes", "note"])
    stage = pick(
        ["stage", "deal_stage", "investment_stage", "pipeline_stage", "status_stage", "deal_status"],
        ["stage", "company_stage"],
    )
    sector = pick(
        ["industry", "sector", "category", "vertical", "market"],
        ["industry", "sector"],
    )
    owner = pick(
        ["owner", "assigned_to", "deal_owner", "responsible", "lead_owner"],
        ["owner", "team"],
    )
    source = pick(["source", "lead_source", "origin", "channel"])
    status = pick(
        ["status", "company_status", "record_status", "deal_status"],
        ["status"],
    )

    attio_url = None
    if attio_record_id:
        attio_url = f"https://app.attio.com/records/{attio_record_id}"

    return {
        "attio_entry_id": entry_id,
        "attio_list_id": list_id,
        "attio_record_id": attio_record_id,
        "name": name,
        "website": website,
        "description": description,
        "stage": stage,
        "sector": sector,
        "owner": owner,
        "source": source,
        "status": status,
        "attio_url": attio_url,
    }


# ---------------------------------------------------------------------------
# Object records  (legacy / fallback sync path)
# ---------------------------------------------------------------------------

def query_attio_company_records(
    object_slug: str, api_key: str, limit: int | None = None
) -> list[dict]:
    """Page through records for the given object. limit caps the total returned."""
    records: list[dict] = []
    offset = 0
    page_size = 500
    while True:
        fetch = page_size if limit is None else min(page_size, limit - len(records))
        try:
            data = _post(
                f"/objects/{object_slug}/records/query",
                api_key,
                {"limit": fetch, "offset": offset},
            )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Records query failed ({exc.response.status_code}): {exc.response.text[:300]}"
            ) from exc
        page = data.get("data", [])
        records.extend(page)
        if len(page) < fetch:
            break
        if limit is not None and len(records) >= limit:
            break
        offset += page_size
    log.info("Fetched %d records from Attio object %r", len(records), object_slug)
    return records


def normalize_attio_record(record: dict) -> dict:
    """Normalise a raw Attio object record into CrmVenture fields."""
    record_id = record.get("id", {})
    if isinstance(record_id, dict):
        record_id = record_id.get("record_id")

    values: dict = record.get("values") or {}

    name = _pick_from(values, ["name", "company_name", "title"])
    website = _pick_from(values, ["domains", "website", "domain", "url"])
    description = _pick_from(values, ["description", "about", "bio", "notes"])
    stage = _pick_from(values, ["stage", "deal_stage", "investment_stage", "pipeline_stage"])
    sector = _pick_from(values, ["industry", "sector", "category", "vertical"])
    owner = _pick_from(values, ["owner", "assigned_to", "deal_owner", "responsible"])
    source = _pick_from(values, ["source", "lead_source", "origin"])
    status = _pick_from(values, ["status", "company_status", "record_status"])

    attio_url = None
    if record_id:
        attio_url = f"https://app.attio.com/records/{record_id}"

    return {
        "attio_record_id": str(record_id) if record_id else None,
        "name": name,
        "website": website,
        "description": description,
        "stage": stage,
        "sector": sector,
        "owner": owner,
        "source": source,
        "status": status,
        "attio_url": attio_url,
    }


# ---------------------------------------------------------------------------
# Notes  (read-only)
# ---------------------------------------------------------------------------

def query_attio_notes_for_record(
    record_id: str, object_slug: str, api_key: str
) -> list[dict]:
    """
    Fetch all notes attached to a specific company/record.
    Kept for backwards compatibility — use query_all_attio_notes for bulk fetches.
    """
    notes: list[dict] = []
    offset = 0
    limit = 20  # Attio notes endpoint max is 20

    while True:
        try:
            resp = httpx.get(
                f"{_BASE}/notes",
                headers=_headers(api_key),
                params={
                    "parent_record_id": record_id,
                    "parent_object": object_slug,
                    "limit": limit,
                    "offset": offset,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return notes
            log.warning(
                "Notes query for record %s failed (%d): %s",
                record_id, exc.response.status_code, exc.response.text[:200],
            )
            return notes
        except Exception as exc:
            log.warning("Notes query for record %s error: %s", record_id, exc)
            return notes

        data = resp.json()
        page = data.get("data", [])
        notes.extend(page)
        if len(page) < limit:
            break
        offset += limit

    return notes


def query_all_attio_notes(api_key: str) -> list[dict]:
    """
    Fetch ALL notes in the workspace in one paginated sweep.
    GET /v2/notes?limit=100&offset=N  (no parent filter)

    Much faster than one call per venture — use this for full syncs.
    Returns list of raw note dicts (metadata only, no body content).
    """
    notes: list[dict] = []
    offset = 0
    limit = 100

    while True:
        try:
            resp = httpx.get(
                f"{_BASE}/notes",
                headers=_headers(api_key),
                params={"limit": limit, "offset": offset},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Bulk notes fetch at offset %d error: %s", offset, exc)
            break

        page = resp.json().get("data", [])
        notes.extend(page)
        if len(page) < limit:
            break
        offset += limit

    log.info("Bulk notes fetch complete: %d notes total", len(notes))
    return notes


def get_attio_note_body(note_id: str, api_key: str) -> str | None:
    """
    Fetch the plaintext body of a note.
    GET /v2/notes/{note_id}

    Returns the plaintext content string, or None on failure.
    """
    try:
        data = _get(f"/notes/{note_id}", api_key)
        note = data.get("data") or data
        # Prefer pre-extracted plaintext
        text = note.get("content_plaintext") or ""
        if text:
            return text.strip() or None
        # Fall back: walk content blocks
        content = note.get("content") or []
        return _extract_text_from_blocks(content) or None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        log.warning("Note body fetch %s failed (%d)", note_id, exc.response.status_code)
        return None
    except Exception as exc:
        log.warning("Note body fetch %s error: %s", note_id, exc)
        return None


def _extract_text_from_blocks(blocks: list) -> str:
    """Recursively extract plain text from Attio rich-content blocks."""
    parts: list[str] = []
    if not isinstance(blocks, list):
        return ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        # Text leaf
        if "text" in block and isinstance(block["text"], str):
            parts.append(block["text"])
        # Inline content
        for key in ("content", "children", "paragraph", "text_value"):
            child = block.get(key)
            if isinstance(child, list):
                parts.append(_extract_text_from_blocks(child))
            elif isinstance(child, dict):
                parts.append(_extract_text_from_blocks([child]))
        # Paragraph / heading separators
        if t in ("paragraph", "heading", "bullet_list_item", "ordered_list_item"):
            parts.append("\n")
    return "".join(parts).strip()


def normalize_attio_note(note: dict, content_text: str | None = None) -> dict:
    """
    Normalise a raw Attio note dict into CrmNote fields.

    note: the raw note object from the list endpoint
    content_text: plaintext body (fetched separately if needed)
    """
    note_id_obj = note.get("id") or {}
    note_id = (
        note_id_obj.get("note_id")
        if isinstance(note_id_obj, dict)
        else str(note_id_obj) if note_id_obj else None
    )

    parent_rid = note.get("parent_record_id")
    if isinstance(parent_rid, dict):
        attio_record_id = parent_rid.get("record_id")
    else:
        attio_record_id = str(parent_rid) if parent_rid else None

    title = (note.get("title") or "").strip() or None

    # Content: prefer passed-in text, then inline plaintext, then extract blocks
    text = content_text
    if not text:
        text = (note.get("content_plaintext") or "").strip() or None
    if not text:
        text = _extract_text_from_blocks(note.get("content") or []) or None

    # Author
    created_by_obj = note.get("created_by") or {}
    created_by: str | None = None
    if isinstance(created_by_obj, dict):
        created_by = (
            created_by_obj.get("name")
            or created_by_obj.get("email_address")
            or created_by_obj.get("workspace_member_id")
        )
        if created_by:
            created_by = str(created_by)

    # Timestamps
    created_at_str = note.get("created_at") or note.get("created_at_attio")
    created_at_attio = None
    if created_at_str:
        try:
            from datetime import datetime
            created_at_attio = datetime.fromisoformat(
                str(created_at_str).replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            pass

    return {
        "attio_note_id": note_id,
        "attio_record_id": attio_record_id,
        "title": title,
        "content_text": text,
        "created_by": created_by,
        "created_at_attio": created_at_attio,
    }


# ---------------------------------------------------------------------------
# Files  (read-only)
# ---------------------------------------------------------------------------

def _extract_files_from_values(values: dict) -> list[dict]:
    """
    Scan an Attio values dict for any attribute that holds file objects.
    Returns a list of raw file value dicts (each has file_id, name, etc.).
    """
    found: list[dict] = []
    if not isinstance(values, dict):
        return found
    for slug, val_list in values.items():
        if not isinstance(val_list, list):
            continue
        for entry in val_list:
            if not isinstance(entry, dict):
                continue
            # Attio file value shape: {"file_id": "...", "name": "...", "content_type": "..."}
            if entry.get("file_id") and entry.get("name"):
                found.append(entry)
    return found


def query_attio_files_for_record(
    record_id: str, object_slug: str, api_key: str
) -> list[dict]:
    """
    Fetch fresh record data and extract file attribute values.
    Returns list of raw file value dicts from the record's values.
    """
    record = get_attio_record(object_slug, record_id, api_key)
    if not record:
        return []
    values = record.get("values") or {}
    return _extract_files_from_values(values)


def query_attio_files_from_raw(raw_record_json: str | None, raw_entry_json: str | None) -> list[dict]:
    """
    Extract file metadata from already-stored raw JSON blobs (no API call).
    Used to discover files without refetching from Attio.
    """
    import json as _json
    found: list[dict] = []
    for blob in (raw_record_json, raw_entry_json):
        if not blob:
            continue
        try:
            data = _json.loads(blob)
        except Exception:
            continue
        for key in ("values", "entry_values"):
            found.extend(_extract_files_from_values(data.get(key) or {}))
    # Deduplicate by file_id
    seen: set[str] = set()
    result: list[dict] = []
    for f in found:
        fid = f.get("file_id")
        if fid and fid not in seen:
            seen.add(fid)
            result.append(f)
    return result


def get_attio_file_download_url(file_id: str, api_key: str) -> str | None:
    """
    Resolve a download URL for an Attio file.
    Tries GET /v2/files/{file_id} first (returns a signed URL).
    Falls back to constructing a direct download path.
    Returns None if unavailable.
    """
    try:
        data = _get(f"/files/{file_id}", api_key)
        url = (
            (data.get("data") or data).get("download_url")
            or (data.get("data") or data).get("url")
        )
        if url:
            return url
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (404, 403):
            log.warning("File URL fetch %s: HTTP %d", file_id, exc.response.status_code)
    except Exception as exc:
        log.warning("File URL fetch %s: %s", file_id, exc)
    return None


def download_attio_file(file_id: str, download_url: str | None, api_key: str) -> bytes | None:
    """
    Download file bytes from Attio.
    Tries the provided download_url first, then resolves a fresh one.
    Returns None on failure.
    """
    urls_to_try: list[str] = []
    if download_url:
        urls_to_try.append(download_url)

    fresh = get_attio_file_download_url(file_id, api_key)
    if fresh and fresh not in urls_to_try:
        urls_to_try.append(fresh)

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=60)
            if resp.status_code == 200:
                return resp.content
            log.warning("File download %s: HTTP %d from %s", file_id, resp.status_code, url[:80])
        except Exception as exc:
            log.warning("File download %s error: %s", file_id, exc)

    return None


def normalize_attio_file(raw: dict) -> dict:
    """
    Normalise a raw Attio file value dict into CrmFile fields.
    raw: one entry from an Attio attribute value list with file_id + name.
    """
    import os
    file_id = raw.get("file_id") or raw.get("id") or ""
    name = (raw.get("name") or raw.get("filename") or "").strip()
    mime = raw.get("content_type") or raw.get("mime_type") or ""
    size = raw.get("size") or raw.get("file_size")

    ext = os.path.splitext(name)[1].lstrip(".").lower() if name else ""
    if not ext and mime:
        _MIME_EXT = {
            "application/pdf": "pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            "application/vnd.ms-excel": "xlsx",
            "text/plain": "txt",
        }
        ext = _MIME_EXT.get(mime, "")

    download_url = raw.get("download_url") or raw.get("url")

    return {
        "attio_file_id": str(file_id) if file_id else None,
        "filename": name or None,
        "file_type": ext or None,
        "mime_type": mime or None,
        "file_size": int(size) if size else None,
        "download_url": download_url or None,
    }


# ---------------------------------------------------------------------------
# People (founder contacts)
# ---------------------------------------------------------------------------

def query_people_for_company(company_record_id: str, api_key: str) -> list[dict]:
    """
    People linked to an Attio company record: [{"name": ..., "email": ...}].
    Fail-safe: returns [] on any error.
    """
    try:
        data = _post(
            "/objects/people/records/query",
            api_key,
            {
                "filter": {
                    "company": {
                        "target_object": "companies",
                        "target_record_id": company_record_id,
                    }
                },
                "limit": 50,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Attio people query failed for company %s: %s", company_record_id, exc)
        return []

    people: list[dict] = []
    for rec in data.get("data", []):
        values = rec.get("values", {})
        name = _pick_value(values.get("name", []) or [])
        if isinstance(name, dict):  # personal-name type: {first_name, last_name, full_name}
            name = name.get("full_name") or " ".join(
                x for x in [name.get("first_name"), name.get("last_name")] if x
            )
        email = _pick_value(values.get("email_addresses", []) or [])
        if isinstance(email, dict):
            email = email.get("email_address") or email.get("original_email_address")
        if name or email:
            people.append({"name": str(name or email), "email": str(email or "")})
    return people
