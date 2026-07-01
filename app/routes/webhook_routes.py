"""
webhook_routes.py — Attio real-time webhook receiver.

Attio calls this endpoint the moment a record or note is created/updated.
We respond immediately (< 1 s) and dispatch the sync+index in a background
thread so we never block the HTTP response.

Setup (one-time, in Attio):
  Workspace settings → Developers → Webhooks → New webhook
  URL:    https://<your-domain>/webhooks/attio
  Events: record.created  record.updated  note.created  note.updated
  Copy the Signing Secret into env var ATTIO_WEBHOOK_SECRET.

Env vars:
  ATTIO_WEBHOOK_SECRET   HMAC-SHA256 signing secret from Attio (recommended)
  ATTIO_API_KEY          Already used elsewhere in the app
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)
router = APIRouter()

ATTIO_WEBHOOK_SECRET = os.environ.get("ATTIO_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(payload: bytes, header: str) -> bool:
    """Verify Attio's HMAC-SHA256 signature.  Skip if no secret is configured."""
    if not ATTIO_WEBHOOK_SECRET:
        log.warning("ATTIO_WEBHOOK_SECRET not set — skipping signature check (insecure)")
        return True
    expected = "sha256=" + hmac.new(
        ATTIO_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header or "")


# ---------------------------------------------------------------------------
# Background workers (one per event type)
# ---------------------------------------------------------------------------

def _handle_note_event(attio_note_id: str) -> None:
    """Sync one Attio note and re-index it. Runs in a background thread."""
    from ..database import SessionLocal
    from ..services.attio_sync import sync_single_note
    from ..services.knowledge_indexer import index_crm_note

    db = SessionLocal()
    try:
        crm_note_id = sync_single_note(attio_note_id, db)
        if crm_note_id:
            index_crm_note(crm_note_id, db)
            log.info("webhook: note %s synced + indexed (crm_note %d)", attio_note_id, crm_note_id)
        else:
            log.warning("webhook: note %s — sync returned no row", attio_note_id)
    except Exception as exc:
        log.error("webhook: note %s failed: %s", attio_note_id, exc, exc_info=True)
    finally:
        db.close()


def _handle_record_event(attio_record_id: str, object_slug: str) -> None:
    """Sync one Attio record and re-index it. Runs in a background thread."""
    from ..database import SessionLocal
    from ..services.attio_sync import sync_single_venture
    from ..services.knowledge_indexer import index_crm_venture
    from ..services.gdrive_ingest import ingest_external_documents

    db = SessionLocal()
    try:
        crm_venture_id = sync_single_venture(attio_record_id, object_slug, db)
        if crm_venture_id:
            index_crm_venture(crm_venture_id, db)
            # Also re-ingest any Drive docs linked to this venture
            try:
                ingest_external_documents(db)
            except Exception as exc:
                log.warning("webhook: gdrive re-ingest skipped: %s", exc)
            log.info(
                "webhook: record %s synced + indexed (crm_venture %d)",
                attio_record_id, crm_venture_id,
            )
        else:
            log.warning("webhook: record %s — sync returned no row", attio_record_id)
    except Exception as exc:
        log.error("webhook: record %s failed: %s", attio_record_id, exc, exc_info=True)
    finally:
        db.close()


def _handle_list_entry_event() -> None:
    """Re-sync the full Attio list when a stage/status changes. Runs in a background thread."""
    from ..database import SessionLocal
    from ..services.attio_sync import sync_attio_list_ventures
    from ..services.knowledge_indexer import index_all_crm_ventures

    db = SessionLocal()
    try:
        log.info("webhook: list-entry change — re-syncing full venture list…")
        sync_attio_list_ventures(db)
        index_all_crm_ventures(db)
        log.info("webhook: list-entry sync complete")
    except Exception as exc:
        log.error("webhook: list-entry sync failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _fire(fn, *args) -> None:
    """Run fn(*args) in a daemon thread — never blocks the HTTP response."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/webhooks/attio", status_code=200)
async def attio_webhook(request: Request):
    """
    Receives Attio webhook events and triggers immediate sync + re-index
    for the affected record or note.
    """
    payload = await request.body()
    signature = request.headers.get("x-attio-client-signature", "")

    if not _verify_signature(payload, signature):
        log.warning("webhook: invalid signature — rejecting")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = await request.json()
    event_type: str = event.get("event_type", "")
    data: dict = event.get("data", {})

    log.info("webhook received: %s", event_type)

    # ── Note events ───────────────────────────────────────────────────────────
    if event_type in ("note.created", "note.updated"):
        id_obj = data.get("id", {})
        note_id = id_obj.get("note_id") if isinstance(id_obj, dict) else None
        if not note_id:
            return {"status": "ignored", "reason": "no note_id in payload"}
        _fire(_handle_note_event, note_id)
        return {"status": "accepted", "event": event_type, "note_id": note_id}

    # ── Record + list-entry events → full list re-sync ───────────────────────
    # All company changes (name, website, stage, status) are handled by
    # re-syncing the full Attio list. This is the only reliable approach since
    # ventures are keyed by attio_entry_id (list entries), not attio_record_id.
    if event_type in (
        "record.created", "record.updated", "record.deleted",
        "list-entry.created", "list-entry.updated", "list-entry.deleted",
    ):
        _fire(_handle_list_entry_event)
        return {"status": "accepted", "event": event_type}

    return {"status": "ignored", "reason": f"unhandled event type: {event_type}"}
