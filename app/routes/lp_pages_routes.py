"""LP page routes: portfolio, sector, companies, company detail, history."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import CrmNote, CrmVenture, LPChatMessage, LPChatSession, LPUser
from ..templates import templates
from .lp_auth_routes import _get_lp_current_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/lp")


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    resp = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _require_user(request: Request, db: Session) -> LPUser | None:
    """Return the LP user or redirect to login."""
    user = _get_lp_current_user(request, db)
    if user is None:
        return None
    return user


def _sector_slug(sector_name: str) -> str:
    return sector_name.lower().replace(" ", "-").replace("/", "-")


# ---------------------------------------------------------------------------
# GET /lp/portfolio
# ---------------------------------------------------------------------------

@router.get("/portfolio", response_class=HTMLResponse)
def lp_portfolio(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    if user is None:
        return RedirectResponse("/lp/login", status_code=303)

    rows = db.execute(
        select(CrmVenture.sector, func.count(CrmVenture.id).label("cnt"))
        .where(func.lower(CrmVenture.stage) == "portfolio")
        .where(CrmVenture.name.is_not(None))
        .where(CrmVenture.sector.is_not(None))
        .group_by(CrmVenture.sector)
        .order_by(func.count(CrmVenture.id).desc())
    ).all()

    sectors = []
    total_companies = 0
    for sector_name, cnt in rows:
        sectors.append({
            "name": sector_name,
            "slug": _sector_slug(sector_name),
            "company_count": cnt,
        })
        total_companies += cnt

    # Also count companies that have no sector (appear in total but not in sectors)
    no_sector_count = db.scalar(
        select(func.count(CrmVenture.id))
        .where(func.lower(CrmVenture.stage) == "portfolio")
        .where(CrmVenture.name.is_not(None))
        .where(CrmVenture.sector.is_(None))
    ) or 0
    total_companies += no_sector_count

    return _render(request, "lp/portfolio.html", {
        "user": user,
        "sectors": sectors,
        "total_sectors": len(sectors),
        "total_companies": total_companies,
    })


# ---------------------------------------------------------------------------
# GET /lp/portfolio/{slug}
# ---------------------------------------------------------------------------

@router.get("/portfolio/{slug}", response_class=HTMLResponse)
def lp_sector(
    slug: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    if user is None:
        return RedirectResponse("/lp/login", status_code=303)

    # Fetch all portfolio companies that have a sector
    all_portfolio = db.scalars(
        select(CrmVenture)
        .where(func.lower(CrmVenture.stage) == "portfolio")
        .where(CrmVenture.name.is_not(None))
        .where(CrmVenture.sector.is_not(None))
        .order_by(CrmVenture.name)
    ).all()

    # Filter by slug
    matching = [c for c in all_portfolio if _sector_slug(c.sector) == slug]

    if not matching:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Sector not found")

    sector_name = matching[0].sector

    return _render(request, "lp/sector.html", {
        "user": user,
        "sector_name": sector_name,
        "slug": slug,
        "companies": matching,
    })


# ---------------------------------------------------------------------------
# GET /lp/companies
# ---------------------------------------------------------------------------

@router.get("/companies", response_class=HTMLResponse)
def lp_companies(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    if user is None:
        return RedirectResponse("/lp/login", status_code=303)

    companies = db.scalars(
        select(CrmVenture)
        .where(func.lower(CrmVenture.stage) == "portfolio")
        .where(CrmVenture.name.is_not(None))
        .order_by(CrmVenture.name)
    ).all()

    return _render(request, "lp/companies.html", {
        "user": user,
        "companies": companies,
    })


# ---------------------------------------------------------------------------
# GET /lp/company/{venture_id}
# ---------------------------------------------------------------------------

@router.get("/company/{venture_id:int}", response_class=HTMLResponse)
def lp_company_detail(
    venture_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    if user is None:
        return RedirectResponse("/lp/login", status_code=303)

    company = db.get(CrmVenture, venture_id)
    if company is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Company not found")

    # Get notes for this company (load content_text explicitly since it's deferred)
    notes = db.scalars(
        select(CrmNote)
        .where(CrmNote.crm_venture_id == venture_id)
        .order_by(CrmNote.created_at_attio.desc().nulls_last())
        .limit(10)
    ).all()
    # Eagerly load content_text for each note
    for note in notes:
        _ = note.content_text

    # Get most recent LP chat session for this user
    session = db.scalar(
        select(LPChatSession)
        .where(LPChatSession.lp_user_id == user.id)
        .order_by(LPChatSession.created_at.desc())
    )

    # Get messages from that session
    enriched_messages = []
    if session:
        messages = db.scalars(
            select(LPChatMessage)
            .where(LPChatMessage.session_id == session.id)
            .order_by(LPChatMessage.created_at.asc())
        ).all()
        enriched_messages = [{"msg": m} for m in messages]

    return _render(request, "lp/company.html", {
        "user": user,
        "company": company,
        "notes": notes,
        "enriched_messages": enriched_messages,
        "session": session,
    })


# ---------------------------------------------------------------------------
# GET /lp/history
# ---------------------------------------------------------------------------

@router.get("/history", response_class=HTMLResponse)
def lp_history(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[LPUser | None, Depends(_get_lp_current_user)],
):
    if user is None:
        return RedirectResponse("/lp/login", status_code=303)

    chat_sessions = db.scalars(
        select(LPChatSession)
        .where(LPChatSession.lp_user_id == user.id)
        .order_by(LPChatSession.created_at.desc())
    ).all()

    sessions = []
    for s in chat_sessions:
        messages = db.scalars(
            select(LPChatMessage)
            .where(LPChatMessage.session_id == s.id)
            .order_by(LPChatMessage.created_at.asc())
        ).all()

        # Build title from first user message
        first_user_msg = next((m for m in messages if m.role == "user"), None)
        if first_user_msg:
            raw = first_user_msg.content.strip()
            title = raw[:60] + ("…" if len(raw) > 60 else "")
        else:
            title = "General conversation"

        # Last message preview
        last_msg = messages[-1] if messages else None
        last_message = ""
        if last_msg:
            raw_last = last_msg.content.strip()
            last_message = raw_last[:120] + ("…" if len(raw_last) > 120 else "")

        # Format date
        updated_at_str = s.created_at.strftime("%b %d, %Y") if s.created_at else ""

        sessions.append({
            "id": s.id,
            "title": title,
            "message_count": len(messages),
            "updated_at_str": updated_at_str,
            "last_message": last_message,
        })

    return _render(request, "lp/history.html", {
        "user": user,
        "sessions": sessions,
    })
