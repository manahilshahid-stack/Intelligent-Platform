"""Admin curation for the LP home page feeds: news approval queue + Luma events."""
from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi import status as _status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..database import get_db
from ..models import NewsItem, PortalEvent, User
from ..templates import templates

router = APIRouter()


@router.get("/admin/news", response_class=HTMLResponse)
def news_page(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    message: str | None = None,
    error: str | None = None,
):
    from datetime import datetime

    from ..config import settings

    pending = db.scalars(
        select(NewsItem).where(NewsItem.status == "pending")
        .order_by(NewsItem.published_at.desc().nullslast(), NewsItem.fetched_at.desc())
    ).all()
    published = db.scalars(
        select(NewsItem).where(NewsItem.status == "approved")
        .order_by(NewsItem.pinned.desc(), NewsItem.published_at.desc().nullslast())
        .limit(60)
    ).all()
    events = db.scalars(
        select(PortalEvent).where(PortalEvent.starts_at >= datetime.utcnow())
        .order_by(PortalEvent.starts_at.asc()).limit(10)
    ).all()

    return templates.TemplateResponse(request, "admin/news.html", {
        "request": request,
        "user": admin,
        "pending": pending,
        "published": published,
        "events": events,
        "luma_configured": bool(settings.luma_api_key),
        "message": message,
        "error": error,
    })


def _redirect(message: str, ok: bool = True) -> RedirectResponse:
    param = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/news?{param}={quote(message)}", status_code=_status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/news/fetch", response_class=HTMLResponse)
def fetch_news_now(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from ..services.news_service import fetch_news
    result = fetch_news(db)
    return _redirect(result["message"], result.get("ok", False))


@router.post("/admin/news/sync-events", response_class=HTMLResponse)
def sync_events_now(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    from ..services.luma_service import sync_luma_events
    result = sync_luma_events(db)
    return _redirect(result["message"], result.get("ok", False))


@router.post("/admin/news/{item_id}/{action}", response_class=HTMLResponse)
def news_item_action(
    item_id: int,
    action: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    item = db.get(NewsItem, item_id)
    if not item:
        return _redirect("News item not found.", ok=False)

    if action == "approve":
        item.status = "approved"
    elif action == "hide":
        item.status = "hidden"
        item.pinned = False
    elif action == "pin":
        item.pinned = True
        item.status = "approved"          # pinning implies approval
    elif action == "unpin":
        item.pinned = False
    else:
        return _redirect(f"Unknown action {action!r}.", ok=False)

    db.commit()
    return _redirect(f"“{item.title[:60]}…” {action}d." if len(item.title) > 60
                     else f"“{item.title}” {action}d.")
