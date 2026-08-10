"""
analytics_routes.py — UX event ingestion and summary API.

Endpoints
---------
POST /api/analytics/events   Batch ingest LP frontend events (fire-and-forget)
GET  /api/analytics/summary  Admin-only aggregate stats for dashboards
GET  /api/analytics/raw      Admin-only paginated raw event log
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..auth import _token_hash
from ..database import get_db
from ..models import AnalyticsEvent, LPUser, LPUserSession

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class EventPayload(BaseModel):
    event_type: str = Field(..., max_length=50)
    event_name: str | None = Field(None, max_length=200)
    session_id: str | None = Field(None, max_length=64)
    page_path: str | None = Field(None, max_length=500)
    referrer_path: str | None = Field(None, max_length=500)
    properties: dict | None = None
    duration_ms: int | None = None


class EventBatch(BaseModel):
    events: list[EventPayload] = Field(..., max_length=50)  # cap 50 per batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_lp_user_id(request: Request, db: Session) -> int | None:
    """Best-effort LP user resolution from Bearer token. Returns None on any failure."""
    try:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        if not token:
            return None
        h = _token_hash(token)
        row = db.scalar(
            select(LPUserSession)
            .where(LPUserSession.token_hash == h)
            .where(LPUserSession.expires_at > datetime.utcnow())
        )
        return row.lp_user_id if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ingest endpoint (public — no auth required, LP user resolved best-effort)
# ---------------------------------------------------------------------------

@router.post("/events", status_code=202)
def ingest_events(
    payload: EventBatch,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Batch-insert analytics events.
    Always returns 202 — errors are logged, never surfaced to the frontend
    so tracking never blocks the UI.
    """
    lp_user_id = _resolve_lp_user_id(request, db)
    try:
        for ev in payload.events:
            props = json.dumps(ev.properties) if ev.properties else None
            db.add(AnalyticsEvent(
                event_type=ev.event_type,
                event_name=ev.event_name,
                lp_user_id=lp_user_id,
                session_id=ev.session_id,
                page_path=ev.page_path,
                referrer_path=ev.referrer_path,
                properties=props,
                duration_ms=ev.duration_ms,
            ))
        db.commit()
        log.debug("analytics: ingested %d events (lp_user=%s)", len(payload.events), lp_user_id)
    except Exception as exc:
        log.warning("analytics ingest failed: %s", exc)
        db.rollback()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin summary (requires admin token via simple header check)
# ---------------------------------------------------------------------------

def _require_admin(request: Request, db: Session) -> None:
    """Lightweight admin check — reuses the existing admin token pattern."""
    from ..routes.auth_routes import get_current_user  # type: ignore
    # Fall back to a simple header check if the import fails
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Admin token required")


@router.get("/summary")
def analytics_summary(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    days: int = 30,
):
    """
    Aggregate stats for the past N days (default 30).
    Returns data ready for Metabase or a custom dashboard.
    """
    since = datetime.utcnow() - timedelta(days=days)

    # Total events
    total = db.scalar(
        select(func.count(AnalyticsEvent.id))
        .where(AnalyticsEvent.created_at >= since)
    ) or 0

    # Unique sessions
    unique_sessions = db.scalar(
        select(func.count(func.distinct(AnalyticsEvent.session_id)))
        .where(AnalyticsEvent.created_at >= since)
    ) or 0

    # Unique LP users
    unique_users = db.scalar(
        select(func.count(func.distinct(AnalyticsEvent.lp_user_id)))
        .where(AnalyticsEvent.created_at >= since)
        .where(AnalyticsEvent.lp_user_id.is_not(None))
    ) or 0

    # Top pages
    top_pages = db.execute(
        select(AnalyticsEvent.page_path, func.count(AnalyticsEvent.id).label("n"))
        .where(AnalyticsEvent.created_at >= since)
        .where(AnalyticsEvent.event_type == "page_view")
        .where(AnalyticsEvent.page_path.is_not(None))
        .group_by(AnalyticsEvent.page_path)
        .order_by(func.count(AnalyticsEvent.id).desc())
        .limit(10)
    ).all()

    # Top companies viewed
    top_companies = db.execute(
        select(AnalyticsEvent.event_name, func.count(AnalyticsEvent.id).label("n"))
        .where(AnalyticsEvent.created_at >= since)
        .where(AnalyticsEvent.event_type == "company_view")
        .where(AnalyticsEvent.event_name.is_not(None))
        .group_by(AnalyticsEvent.event_name)
        .order_by(func.count(AnalyticsEvent.id).desc())
        .limit(10)
    ).all()

    # Events by type breakdown
    by_type = db.execute(
        select(AnalyticsEvent.event_type, func.count(AnalyticsEvent.id).label("n"))
        .where(AnalyticsEvent.created_at >= since)
        .group_by(AnalyticsEvent.event_type)
        .order_by(func.count(AnalyticsEvent.id).desc())
    ).all()

    # Daily active users (sessions with at least 1 page_view per day)
    dau_rows = db.execute(
        text("""
            SELECT
                DATE(created_at) AS day,
                COUNT(DISTINCT session_id) AS sessions,
                COUNT(DISTINCT lp_user_id) AS users
            FROM analytics_events
            WHERE created_at >= :since
              AND event_type = 'page_view'
            GROUP BY DATE(created_at)
            ORDER BY day DESC
        """),
        {"since": since},
    ).all()

    # Avg time on page (where duration_ms captured)
    avg_duration = db.scalar(
        select(func.avg(AnalyticsEvent.duration_ms))
        .where(AnalyticsEvent.created_at >= since)
        .where(AnalyticsEvent.duration_ms.is_not(None))
        .where(AnalyticsEvent.event_type == "page_view")
    )

    return {
        "period_days": days,
        "total_events": total,
        "unique_sessions": unique_sessions,
        "unique_users": unique_users,
        "avg_page_duration_ms": round(avg_duration) if avg_duration else None,
        "top_pages": [{"path": r.page_path, "views": r.n} for r in top_pages],
        "top_companies": [{"company": r.event_name, "views": r.n} for r in top_companies],
        "events_by_type": [{"type": r.event_type, "count": r.n} for r in by_type],
        "daily_activity": [
            {"day": str(r.day), "sessions": r.sessions, "users": r.users}
            for r in dau_rows
        ],
    }


@router.get("/raw")
def analytics_raw(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    event_type: str | None = None,
    days: int = 7,
    offset: int = 0,
    limit: int = 100,
):
    """Paginated raw event log for admin inspection."""
    since = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(AnalyticsEvent)
        .where(AnalyticsEvent.created_at >= since)
        .order_by(AnalyticsEvent.created_at.desc())
        .offset(offset)
        .limit(min(limit, 500))
    )
    if event_type:
        stmt = stmt.where(AnalyticsEvent.event_type == event_type)

    rows = db.scalars(stmt).all()
    return {
        "events": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "event_name": r.event_name,
                "lp_user_id": r.lp_user_id,
                "session_id": r.session_id,
                "page_path": r.page_path,
                "referrer_path": r.referrer_path,
                "properties": json.loads(r.properties) if r.properties else None,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }
