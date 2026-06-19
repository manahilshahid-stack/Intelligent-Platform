"""
Admin CRM routes — Attio settings, sync, venture list and detail.

All routes require admin role.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import CrmSyncRun, CrmSyncStatus, CrmVenture, User
from ..services.settings_service import (
    get_attio_api_key,
    get_attio_list_id_or_slug,
    get_attio_object_slug,
    has_env_attio_key,
    mask_key,
    set_attio_api_key,
    set_attio_list_id_or_slug,
    set_attio_object_slug,
)
from ..templates import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/crm")


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ---------------------------------------------------------------------------
# Attio settings (embedded in /admin/settings but also has dedicated POST routes)
# ---------------------------------------------------------------------------

@router.post("/settings", response_class=HTMLResponse)
async def save_attio_settings(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    """Save Attio API key + object slug + list id."""
    form = await request.form()
    api_key = (form.get("attio_api_key") or "").strip()
    object_slug = (form.get("attio_object_slug") or "companies").strip()
    list_id = (form.get("attio_list_id_or_slug") or "").strip()

    if api_key:
        set_attio_api_key(api_key, db)

    set_attio_object_slug(object_slug or "companies", db)
    set_attio_list_id_or_slug(list_id or None, db)

    log.info("Attio settings updated by admin %d", admin.id)
    return RedirectResponse("/admin/settings?attio_saved=1", status_code=303)


@router.post("/test", response_class=HTMLResponse)
def test_attio_connection(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from ..services.attio_client import test_attio_connection as _test
    api_key = get_attio_api_key(db)
    if not api_key:
        return RedirectResponse("/admin/settings?attio_error=no_key", status_code=303)

    ok, msg = _test(api_key)
    status_param = "ok" if ok else "fail"
    import urllib.parse
    msg_enc = urllib.parse.quote(msg)
    return RedirectResponse(
        f"/admin/settings?attio_test={status_param}&attio_msg={msg_enc}",
        status_code=303,
    )


@router.post("/fields", response_class=HTMLResponse)
def fetch_attio_fields(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from ..services.attio_client import list_attio_object_attributes
    api_key = get_attio_api_key(db)
    object_slug = get_attio_object_slug(db)

    if not api_key:
        return RedirectResponse("/admin/settings?attio_error=no_key", status_code=303)

    try:
        attrs = list_attio_object_attributes(object_slug, api_key)
        attrs_json = json.dumps(attrs, ensure_ascii=False)
        import urllib.parse
        return RedirectResponse(
            f"/admin/settings?attio_fields={urllib.parse.quote(attrs_json)}",
            status_code=303,
        )
    except Exception as exc:
        import urllib.parse
        return RedirectResponse(
            f"/admin/settings?attio_error={urllib.parse.quote(str(exc)[:300])}",
            status_code=303,
        )


@router.post("/reindex", response_class=HTMLResponse)
def trigger_reindex(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    import urllib.parse
    from ..services.knowledge_indexer import index_all_crm_ventures
    try:
        result = index_all_crm_ventures(db)
        msg = urllib.parse.quote(
            f"Re-indexed {result['indexed']} ventures"
            + (f" ({result['errors']} errors)" if result["errors"] else "")
        )
        return RedirectResponse(f"/admin/crm/ventures?reindex_ok={msg}", status_code=303)
    except Exception as exc:
        err = urllib.parse.quote(str(exc)[:300])
        return RedirectResponse(f"/admin/crm/ventures?reindex_error={err}", status_code=303)


@router.post("/gdrive/ingest", response_class=HTMLResponse)
def trigger_gdrive_ingest(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    """Fetch + index the Google Drive/Docs documents linked across the CRM."""
    import urllib.parse
    from ..services.gdrive_ingest import ingest_external_documents
    try:
        r = ingest_external_documents(db)
        msg = urllib.parse.quote(
            f"Google docs: {r['fetched']} fetched, {r['indexed_chunks']} chunks indexed, "
            f"{r['no_access']} no-access, {r['unsupported']} unsupported, {r['failed']} failed "
            f"(of {r['links']} links)"
        )
        return RedirectResponse(f"/admin/crm/ventures?reindex_ok={msg}", status_code=303)
    except Exception as exc:
        err = urllib.parse.quote(str(exc)[:300])
        return RedirectResponse(f"/admin/crm/ventures?reindex_error={err}", status_code=303)


@router.post("/notes/sync", response_class=HTMLResponse)
def trigger_notes_sync(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    import threading
    from ..models import CrmSyncRun, CrmSyncStatus
    from datetime import datetime

    run = CrmSyncRun(
        sync_type="notes_sync",
        status=CrmSyncStatus.running,
        started_at=datetime.utcnow(),
        records_seen=0, records_created=0, records_updated=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    def _do():
        from ..database import SessionLocal
        from ..services.attio_sync import sync_attio_notes_for_ventures, _fail_run
        bg = SessionLocal()
        try:
            bg_run = bg.get(CrmSyncRun, run_id)
            sync_attio_notes_for_ventures(bg, run=bg_run)
        except Exception as exc:
            log.error("Notes sync background error: %s", exc, exc_info=True)
            try:
                bg_run = bg.get(CrmSyncRun, run_id)
                if bg_run and bg_run.status == CrmSyncStatus.running:
                    _fail_run(bg_run, str(exc)[:2000], bg)
            except Exception:
                pass
        finally:
            bg.close()

    threading.Thread(target=_do, daemon=True).start()
    return RedirectResponse(f"/admin/crm/sync/progress/{run_id}", status_code=303)


@router.post("/notes/reindex", response_class=HTMLResponse)
def trigger_notes_reindex(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    import threading
    from ..models import CrmSyncRun, CrmSyncStatus
    from datetime import datetime

    run = CrmSyncRun(
        sync_type="notes_reindex",
        status=CrmSyncStatus.running,
        started_at=datetime.utcnow(),
        records_seen=0, records_created=0, records_updated=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    def _do():
        from ..database import SessionLocal
        from ..services.knowledge_indexer import index_all_crm_notes
        from ..services.attio_sync import _finish_run, _fail_run
        bg = SessionLocal()
        try:
            bg_run = bg.get(CrmSyncRun, run_id)
            result = index_all_crm_notes(bg)
            if bg_run:
                _finish_run(bg_run, result.get("indexed", 0), result.get("indexed", 0), 0, bg)
        except Exception as exc:
            log.error("Notes reindex background error: %s", exc, exc_info=True)
            try:
                bg_run = bg.get(CrmSyncRun, run_id)
                if bg_run and bg_run.status == CrmSyncStatus.running:
                    _fail_run(bg_run, str(exc)[:2000], bg)
            except Exception:
                pass
        finally:
            bg.close()

    threading.Thread(target=_do, daemon=True).start()
    return RedirectResponse(f"/admin/crm/sync/progress/{run_id}", status_code=303)


@router.post("/files/sync", response_class=HTMLResponse)
def trigger_files_sync(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    import threading
    from ..models import CrmSyncRun, CrmSyncStatus
    from datetime import datetime

    run = CrmSyncRun(
        sync_type="files_sync",
        status=CrmSyncStatus.running,
        started_at=datetime.utcnow(),
        records_seen=0, records_created=0, records_updated=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    def _do():
        from ..database import SessionLocal
        from ..services.attio_sync import sync_attio_files_for_ventures, _fail_run
        bg = SessionLocal()
        try:
            bg_run = bg.get(CrmSyncRun, run_id)
            sync_attio_files_for_ventures(bg, run=bg_run)
        except Exception as exc:
            log.error("Files sync background error: %s", exc, exc_info=True)
            try:
                bg_run = bg.get(CrmSyncRun, run_id)
                if bg_run and bg_run.status == CrmSyncStatus.running:
                    _fail_run(bg_run, str(exc)[:2000], bg)
            except Exception:
                pass
        finally:
            bg.close()

    threading.Thread(target=_do, daemon=True).start()
    return RedirectResponse(f"/admin/crm/sync/progress/{run_id}", status_code=303)


@router.post("/files/reindex", response_class=HTMLResponse)
def trigger_files_reindex(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    import threading
    from ..models import CrmSyncRun, CrmSyncStatus
    from datetime import datetime

    run = CrmSyncRun(
        sync_type="files_reindex",
        status=CrmSyncStatus.running,
        started_at=datetime.utcnow(),
        records_seen=0, records_created=0, records_updated=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    def _do():
        from ..database import SessionLocal
        from ..services.knowledge_indexer import index_all_crm_files
        from ..services.attio_sync import _finish_run, _fail_run
        bg = SessionLocal()
        try:
            bg_run = bg.get(CrmSyncRun, run_id)
            result = index_all_crm_files(bg)
            if bg_run:
                _finish_run(bg_run, result.get("indexed", 0), result.get("indexed", 0), 0, bg)
        except Exception as exc:
            log.error("Files reindex background error: %s", exc, exc_info=True)
            try:
                bg_run = bg.get(CrmSyncRun, run_id)
                if bg_run and bg_run.status == CrmSyncStatus.running:
                    _fail_run(bg_run, str(exc)[:2000], bg)
            except Exception:
                pass
        finally:
            bg.close()

    threading.Thread(target=_do, daemon=True).start()
    return RedirectResponse(f"/admin/crm/sync/progress/{run_id}", status_code=303)


@router.get("/files", response_class=HTMLResponse)
def list_crm_files(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    venture_id: int | None = None,
    file_type: str = "",
    status: str = "",
    q: str = "",
):
    from sqlalchemy import select as _sel, func
    from ..models import CrmFile, CrmVenture

    stmt = _sel(CrmFile)
    if venture_id:
        stmt = stmt.where(CrmFile.crm_venture_id == venture_id)
    if file_type:
        stmt = stmt.where(CrmFile.file_type == file_type)
    if status:
        from ..models import CrmFileStatus as _S
        try:
            stmt = stmt.where(CrmFile.extraction_status == _S(status))
        except ValueError:
            pass
    if q:
        stmt = stmt.where(CrmFile.filename.ilike(f"%{q}%"))

    stmt = stmt.order_by(CrmFile.synced_at.desc())
    files = db.scalars(stmt).all()

    all_ventures = db.scalars(_sel(CrmVenture).order_by(CrmVenture.name)).all()
    all_types = [r[0] for r in db.execute(
        _sel(CrmFile.file_type).distinct().where(CrmFile.file_type.isnot(None)).order_by(CrmFile.file_type)
    ).all()]
    all_statuses = [
        r[0].value for r in db.execute(
            _sel(CrmFile.extraction_status).distinct().where(CrmFile.extraction_status.isnot(None))
        ).all() if r[0] is not None
    ]

    from ..templates import templates
    return templates.TemplateResponse("admin/crm_files.html", {
        "request": request,
        "files": files,
        "total": len(files),
        "all_ventures": all_ventures,
        "all_types": all_types,
        "all_statuses": all_statuses,
        "filter_venture_id": venture_id,
        "filter_file_type": file_type,
        "filter_status": status,
        "filter_q": q,
    })


@router.post("/sync", response_class=HTMLResponse)
async def trigger_sync(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    import threading
    from datetime import datetime
    from ..models import CrmSyncRun, CrmSyncStatus

    form = await request.form()
    raw_limit = (form.get("sync_limit") or "").strip()
    limit: int | None = None
    if raw_limit:
        try:
            limit = max(1, int(raw_limit))
        except ValueError:
            pass

    list_id = get_attio_list_id_or_slug(db)

    # Create the run row now so we have an ID to redirect to immediately
    run = CrmSyncRun(
        sync_type="attio_list" if list_id else "attio_object",
        status=CrmSyncStatus.running,
        started_at=datetime.utcnow(),
        records_seen=0,
        records_created=0,
        records_updated=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    def _do_sync():
        from ..services.attio_sync import resume_run
        resume_run(run_id, limit=limit)

    thread = threading.Thread(target=_do_sync, daemon=True)
    thread.start()

    return RedirectResponse(f"/admin/crm/sync/progress/{run_id}", status_code=303)


@router.get("/sync/progress/{run_id}", response_class=HTMLResponse)
def sync_progress_page(
    run_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    run = db.get(CrmSyncRun, run_id)
    if not run:
        return RedirectResponse("/admin/crm/ventures", status_code=303)
    return _render(request, "admin/crm_sync_progress.html", {
        "user": admin,
        "run": run,
    })


@router.get("/sync/progress/{run_id}/json")
def sync_progress_json(
    run_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from fastapi.responses import JSONResponse
    # Expire cached state so we see fresh DB values
    db.expire_all()
    run = db.get(CrmSyncRun, run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    total = run.records_total
    seen = run.records_seen or 0
    pct = round(seen / total * 100) if total else 0
    return JSONResponse({
        "status": run.status.value,
        "records_total": total,
        "records_seen": seen,
        "records_created": run.records_created or 0,
        "records_updated": run.records_updated or 0,
        "pct": pct,
        "error": run.error,
    })


# ---------------------------------------------------------------------------
# CRM venture list  GET /admin/crm/ventures
# ---------------------------------------------------------------------------

@router.get("/ventures", response_class=HTMLResponse)
def crm_ventures_list(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    q: str | None = None,
    stage: str | None = None,
    sector: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    sync_run: int | None = None,
    sync_error: str | None = None,
    reindex_ok: str | None = None,
    reindex_error: str | None = None,
):
    query = select(CrmVenture).order_by(CrmVenture.name)

    if q:
        like = f"%{q}%"
        from sqlalchemy import or_
        query = query.where(
            or_(
                CrmVenture.name.ilike(like),
                CrmVenture.website.ilike(like),
                CrmVenture.description.ilike(like),
            )
        )
    if stage:
        query = query.where(CrmVenture.stage == stage)
    if sector:
        query = query.where(CrmVenture.sector == sector)
    if owner:
        query = query.where(CrmVenture.owner == owner)
    if status:
        query = query.where(CrmVenture.status == status)

    ventures = list(db.scalars(query).all())

    # Filter options
    def _distinct(col):
        return sorted(
            r[0] for r in db.execute(select(col).distinct().where(col.is_not(None))).all()
            if r[0]
        )

    all_stages = _distinct(CrmVenture.stage)
    all_sectors = _distinct(CrmVenture.sector)
    all_owners = _distinct(CrmVenture.owner)
    all_statuses = _distinct(CrmVenture.status)

    # Last sync run
    last_run = db.scalar(
        select(CrmSyncRun).order_by(CrmSyncRun.started_at.desc())
    )

    # Sync run info for banner
    sync_run_obj = db.get(CrmSyncRun, sync_run) if sync_run else None

    return _render(request, "admin/crm_ventures.html", {
        "user": admin,
        "ventures": ventures,
        "total": len(ventures),
        "all_stages": all_stages,
        "all_sectors": all_sectors,
        "all_owners": all_owners,
        "all_statuses": all_statuses,
        "filter_q": q or "",
        "filter_stage": stage or "",
        "filter_sector": sector or "",
        "filter_owner": owner or "",
        "filter_status": status or "",
        "last_run": last_run,
        "sync_run": sync_run_obj,
        "sync_error": sync_error,
        "reindex_ok": reindex_ok,
        "reindex_error": reindex_error,
    })


# ---------------------------------------------------------------------------
# CRM venture detail  GET /admin/crm/ventures/{id}
# ---------------------------------------------------------------------------

@router.get("/ventures/{venture_id}", response_class=HTMLResponse)
def crm_venture_detail(
    venture_id: int,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from fastapi import HTTPException
    from sqlalchemy import select as _sel
    from ..models import CrmNote, CrmFile

    venture = db.get(CrmVenture, venture_id)
    if not venture:
        raise HTTPException(status_code=404, detail="CRM venture not found.")

    notes = list(
        db.scalars(
            _sel(CrmNote)
            .where(CrmNote.crm_venture_id == venture_id)
            .order_by(CrmNote.created_at_attio.desc().nullslast())
        ).all()
    )

    files = list(
        db.scalars(
            _sel(CrmFile)
            .where(CrmFile.crm_venture_id == venture_id)
            .order_by(CrmFile.synced_at.desc().nullslast())
        ).all()
    )

    def _pretty(src: str | None) -> str | None:
        if not src:
            return None
        try:
            return json.dumps(json.loads(src), indent=2, ensure_ascii=False)
        except Exception:
            return src

    return _render(request, "admin/crm_venture_detail.html", {
        "user": admin,
        "venture": venture,
        "notes": notes,
        "files": files,
        "raw_entry_json_pretty": _pretty(venture.raw_entry_json),
        "raw_record_json_pretty": _pretty(venture.raw_record_json),
        "raw_attio_json_pretty": _pretty(venture.raw_attio_json),
    })




@router.post("/sync/kill-all")
def kill_all_syncs(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    """Mark all running sync runs as failed so progress pages stop polling."""
    from fastapi.responses import JSONResponse
    from datetime import datetime

    running = db.scalars(
        select(CrmSyncRun).where(CrmSyncRun.status == CrmSyncStatus.running)
    ).all()
    killed = []
    for run in running:
        run.status = CrmSyncStatus.failed
        run.finished_at = datetime.utcnow()
        run.error = "Manually killed"
        killed.append(run.id)
    db.commit()
    return JSONResponse({"killed_run_ids": killed})
