"""Admin settings routes."""
from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import User
from ..services.settings_service import (
    get_attio_api_key,
    get_attio_list_id_or_slug,
    get_attio_object_slug,
    get_openrouter_api_key,
    has_env_attio_key,
    has_env_openrouter_key,
    mask_key,
    set_openrouter_api_key,
)
from ..templates import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings")


def _render(request: Request, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, "admin/settings.html", ctx, status_code=status_code)


def _test_openrouter_key(api_key: str) -> tuple[bool, str]:
    """
    Send a minimal request to OpenRouter to verify the key is accepted.
    Returns (ok, message).
    """
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1,
            },
            timeout=15,
        )
    except httpx.RequestError as exc:
        return False, f"Network error: {exc}"

    if resp.status_code == 200:
        return True, "Connection successful."
    if resp.status_code == 401:
        return False, "Invalid API key (401 Unauthorized). Check that the key is correct and not expired."
    if resp.status_code == 402:
        return False, "Insufficient credits (402). Top up your OpenRouter account."
    if resp.status_code == 429:
        # Rate-limited but key is valid
        return True, "Key accepted (rate-limited — slow down requests if this persists)."
    try:
        detail = resp.json().get("error", {}).get("message", resp.text[:200])
    except Exception:
        detail = resp.text[:200]
    return False, f"Unexpected response {resp.status_code}: {detail}"


def _page_ctx(
    admin: User,
    db: Session,
    *,
    error=None,
    success=None,
    test_result=None,
    attio_test=None,
    attio_msg=None,
    attio_error=None,
    attio_fields=None,
    attio_saved=False,
) -> dict:
    env_active = has_env_openrouter_key()
    active_key = get_openrouter_api_key(db)
    db_key = None if env_active else active_key

    attio_key = get_attio_api_key(db)
    attio_env_active = has_env_attio_key()

    return {
        "user": admin,
        "env_active": env_active,
        "masked_db_key": mask_key(db_key),
        "masked_active_key": mask_key(active_key),
        "has_active_key": bool(active_key),
        "error": error,
        "success": success,
        "test_result": test_result,
        # Attio
        "attio_has_key": bool(attio_key),
        "attio_masked_key": mask_key(attio_key),
        "attio_env_active": attio_env_active,
        "attio_object_slug": get_attio_object_slug(db),
        "attio_list_id_or_slug": get_attio_list_id_or_slug(db) or "",
        "attio_test": attio_test,
        "attio_msg": attio_msg,
        "attio_error": attio_error,
        "attio_fields": attio_fields,
        "attio_saved": attio_saved,
    }


# ---------------------------------------------------------------------------
# GET /admin/settings
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    attio_test: str | None = None,
    attio_msg: str | None = None,
    attio_error: str | None = None,
    attio_fields: str | None = None,
    attio_saved: str | None = None,
):
    import json as _json
    parsed_fields = None
    if attio_fields:
        try:
            parsed_fields = _json.loads(attio_fields)
        except Exception:
            pass

    return _render(request, _page_ctx(
        admin, db,
        attio_test=attio_test,
        attio_msg=attio_msg,
        attio_error=attio_error,
        attio_fields=parsed_fields,
        attio_saved=bool(attio_saved),
    ))


# ---------------------------------------------------------------------------
# POST /admin/settings/openrouter  — save key
# ---------------------------------------------------------------------------

@router.post("/openrouter", response_class=HTMLResponse)
def save_openrouter_key(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    api_key: Annotated[str, Form()] = "",
):
    api_key = api_key.strip()
    if not api_key:
        return _render(request, _page_ctx(admin, db, error="API key must not be empty."), 400)

    set_openrouter_api_key(api_key, db)
    log.info("OpenRouter API key updated by admin user %d", admin.id)

    # Auto-test the key immediately after saving
    ok, msg = _test_openrouter_key(api_key)
    test_result = {"ok": ok, "message": msg}
    success = "Key saved." if ok else "Key saved, but the connection test failed — see below."

    return _render(request, _page_ctx(admin, db, success=success, test_result=test_result))


# ---------------------------------------------------------------------------
# POST /admin/settings/test  — test current active key
# ---------------------------------------------------------------------------

@router.post("/test", response_class=HTMLResponse)
def test_connection(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    api_key = get_openrouter_api_key(db)
    if not api_key:
        return _render(request, _page_ctx(admin, db,
            error="No API key is configured. Save a key first."))

    ok, msg = _test_openrouter_key(api_key)
    test_result = {"ok": ok, "message": msg}
    return _render(request, _page_ctx(admin, db, test_result=test_result))
