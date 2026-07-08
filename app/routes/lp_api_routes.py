"""
lp_api_routes.py — JSON REST API for the React LP frontend.

All endpoints return JSON (not HTML). Auth uses Bearer tokens so the React
app can run on a different domain from the FastAPI backend.

Endpoints:
  POST /api/lp/register                 Create account → return bearer token immediately
  POST /api/lp/login                    Email + password → return bearer token
  GET  /api/lp/me                       Get current user profile
  PUT  /api/lp/me                       Update profile / complete onboarding
  POST /api/lp/chat                     Send a message, get AI response
  POST /api/lp/logout                   Invalidate session
  GET  /api/lp/companies                List all portfolio companies
  GET  /api/lp/companies/{id}           Single company detail
  GET  /api/lp/portfolio/sectors        Portfolio grouped by sector
  GET  /api/lp/portfolio/sectors/{slug} Companies in a sector
  GET  /api/lp/chat/sessions            All chat sessions for current user
  GET  /api/lp/chat/sessions/{id}       Single session with messages
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password, _token_hash
from ..config import settings
from ..database import get_db
from ..models import AppSetting, LPUser, LPUserSession

log = logging.getLogger(__name__)

# ── Company name aliases ──────────────────────────────────────────────────────
# Maps current name → list of former names.
# Used for query expansion (search for old names too) and AI context injection.
# Update this whenever a portfolio company is renamed in Attio.
COMPANY_ALIASES: dict[str, list[str]] = {
    "almetra":          ["deltia", "deltia.ai"],
    "whistle robotics": ["foundry robotics", "foundary robotics"],
    "revel8":           ["company shield"],
}

# Flat reverse map: old name → current name (for display correction)
_OLD_TO_NEW: dict[str, str] = {
    old.lower(): new
    for new, olds in COMPANY_ALIASES.items()
    for old in olds
}

def _expand_query_with_aliases(query: str) -> str:
    """Append old company names to the search query so embeddings match historical notes."""
    q_lower = query.lower()
    extras: list[str] = []
    for current, old_names in COMPANY_ALIASES.items():
        if current in q_lower:
            extras.extend(old_names)
    if extras:
        return query + " " + " ".join(extras)
    return query

def _build_alias_context() -> str:
    """Build a context note about company renames to inject into the system prompt."""
    if not COMPANY_ALIASES:
        return ""
    lines = ["COMPANY NAME CHANGES (use the current name in all responses):"]
    for current, old_names in COMPANY_ALIASES.items():
        lines.append(f"- {current.title()} was formerly known as: {', '.join(n.title() for n in old_names)}")
    return "\n".join(lines)
router = APIRouter(prefix="/api/lp")

_COLORS = [
    "oklch(0.92 0.25 120)",
    "oklch(0.72 0.21 55)",
    "oklch(0.18 0.01 60)",
    "oklch(0.65 0.12 85)",
]


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _create_token(lp_user: LPUser, db: Session) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=settings.session_ttl_hours)
    db.add(LPUserSession(
        lp_user_id=lp_user.id,
        token_hash=_token_hash(token),
        expires_at=expires,
    ))
    db.commit()
    return token


def _get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> LPUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    h = _token_hash(token)
    row = db.scalar(
        select(LPUserSession)
        .where(LPUserSession.token_hash == h)
        .where(LPUserSession.expires_at > datetime.utcnow())
    )
    if row is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    user = db.get(LPUser, row.lp_user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def _user_to_dict(user: LPUser) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "organization": user.organization,
        "interest_areas": json.loads(user.interest_areas) if user.interest_areas else [],
        "looking_for": json.loads(user.looking_for) if user.looking_for else [],
        "about_yourself": user.about_yourself or "",
        "onboarding_completed": user.onboarding_completed,
        "avatar": None,
    }


def _sector_slug(sector_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sector_name.lower()).strip("-")


def _venture_to_dict(v, index: int = 0) -> dict:
    from ..models import CrmVenture
    website = (v.website or "").replace("https://", "").replace("http://", "").rstrip("/")
    return {
        "id": str(v.id),
        "name": v.name,
        "tagline": v.description or f"{v.name} — Merantix Capital portfolio company",
        "category": v.sector or "Deep Tech",
        "stage": "Seed",
        "founders": [],
        "website": website,
        "status": "Active",
        "logo": (v.name or "?")[0].upper(),
        "color": _COLORS[index % 4],
        "hq": "",
        "fund": "Merantix Capital",
        "investmentYear": 2024,
        "valuation": 0,
        "invested": 0,
        "growth": 0,
    }


# ── Request / response models ─────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    email: str
    company: str = ""
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class UpdateProfileRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    interest_areas: list[str] | None = None
    looking_for: list[str] | None = None
    about_yourself: str | None = None
    onboarding_completed: bool | None = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | int | None = None
    company_name: str | None = None  # set by company detail page to focus retrieval


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.post("/register")
def api_register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new LP account and return a bearer token immediately."""
    email = body.email.strip().lower()
    first = body.first_name.strip()
    last = body.last_name.strip()
    password = body.password.strip()

    if not first or not last or not email or not password:
        raise HTTPException(400, "First name, last name, email and password are required")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if db.scalar(select(LPUser).where(LPUser.email == email)):
        raise HTTPException(400, "An account with this email already exists")

    name = f"{first} {last}".strip()
    user = LPUser(
        name=name,
        email=email,
        organization=body.company.strip() or None,
        password_hash=hash_password(password),
        onboarding_completed=False,
    )
    db.add(user)
    db.commit()

    token = _create_token(user, db)
    return {"ok": True, "token": token, "user": _user_to_dict(user)}


@router.post("/login")
def api_login(body: LoginRequest, db: Session = Depends(get_db)):
    """Login with email + password → return bearer token."""
    email = body.email.strip().lower()
    user = db.scalar(select(LPUser).where(LPUser.email == email))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")

    token = _create_token(user, db)
    return {"ok": True, "token": token, "user": _user_to_dict(user)}


@router.get("/me")
def api_get_me(current_user: LPUser = Depends(_get_current_user)):
    """Get current user profile."""
    return _user_to_dict(current_user)


@router.put("/me")
def api_update_me(
    body: UpdateProfileRequest,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Update profile / complete onboarding."""
    if body.first_name is not None or body.last_name is not None:
        parts = current_user.name.split()
        first = body.first_name if body.first_name is not None else (parts[0] if parts else "")
        last = body.last_name if body.last_name is not None else (" ".join(parts[1:]) if len(parts) > 1 else "")
        current_user.name = f"{first} {last}".strip()
    if body.company is not None:
        current_user.organization = body.company or None
    if body.interest_areas is not None:
        current_user.interest_areas = json.dumps(body.interest_areas)
    if body.looking_for is not None:
        current_user.looking_for = json.dumps(body.looking_for)
    if body.about_yourself is not None:
        current_user.about_yourself = body.about_yourself or None
    if body.onboarding_completed is not None:
        current_user.onboarding_completed = body.onboarding_completed
    db.commit()
    return _user_to_dict(current_user)


@router.post("/logout")
def api_logout(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
):
    """Invalidate the bearer token."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        try:
            db.query(LPUserSession).filter(
                LPUserSession.token_hash == _token_hash(token)
            ).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
    return {"ok": True}


# ── Company endpoints ─────────────────────────────────────────────────────────

@router.get("/public/companies")
def api_get_public_companies(db: Session = Depends(get_db)):
    """Public endpoint — returns just names and sectors for the landing page marquee. No auth required."""
    from ..models import CrmVenture
    rows = db.execute(
        select(CrmVenture.name, CrmVenture.sector)
        .where(func.lower(CrmVenture.stage).contains("portfolio"))
        .where(CrmVenture.name.is_not(None))
        .order_by(CrmVenture.name)
    ).all()
    return [{"name": r[0], "sector": r[1] or "Deep Tech"} for r in rows]


@router.get("/companies")
def api_get_companies(
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Return all portfolio companies."""
    from ..models import CrmVenture
    ventures = db.scalars(
        select(CrmVenture)
        .where(func.lower(CrmVenture.stage).contains("portfolio"))
        .where(CrmVenture.name.is_not(None))
        .order_by(CrmVenture.name)
    ).all()
    return [_venture_to_dict(v, i) for i, v in enumerate(ventures)]


@router.get("/companies/{company_id}")
def api_get_company(
    company_id: int,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Return a single portfolio company by ID."""
    from ..models import CrmVenture
    v = db.get(CrmVenture, company_id)
    if v is None or "portfolio" not in (v.stage or "").lower():
        raise HTTPException(404, "Company not found")
    return _venture_to_dict(v, company_id % 4)


# ── Portfolio / sector endpoints ──────────────────────────────────────────────

@router.get("/portfolio/sectors")
def api_get_sectors(
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Return portfolio sectors with company counts."""
    from ..models import CrmVenture
    rows = db.execute(
        select(CrmVenture.sector, func.count(CrmVenture.id).label("cnt"))
        .where(func.lower(CrmVenture.stage).contains("portfolio"))
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
            "investigation_count": 0,
        })
        total_companies += cnt

    no_sector_count = db.scalar(
        select(func.count(CrmVenture.id))
        .where(func.lower(CrmVenture.stage).contains("portfolio"))
        .where(CrmVenture.name.is_not(None))
        .where(CrmVenture.sector.is_(None))
    ) or 0

    return {
        "sectors": sectors,
        "total_sectors": len(sectors),
        "total_companies": total_companies + no_sector_count,
    }


@router.get("/portfolio/sectors/{slug}")
def api_get_sector(
    slug: str,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Return companies in a sector by slug."""
    from ..models import CrmVenture
    all_portfolio = db.scalars(
        select(CrmVenture)
        .where(func.lower(CrmVenture.stage).contains("portfolio"))
        .where(CrmVenture.name.is_not(None))
        .where(CrmVenture.sector.is_not(None))
        .order_by(CrmVenture.name)
    ).all()

    matching = [v for v in all_portfolio if _sector_slug(v.sector) == slug]
    if not matching:
        raise HTTPException(404, "Sector not found")

    return {
        "sector_name": matching[0].sector,
        "slug": slug,
        "companies": [_venture_to_dict(v, i) for i, v in enumerate(matching)],
        "investigations": [],
    }


# ── Chat endpoints ────────────────────────────────────────────────────────────

@router.post("/chat")
def api_chat(
    body: ChatRequest,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message to the LP AI analyst and return the response."""
    from ..models import LPChatSession, LPChatMessage, User, UserRole
    from ..services.retrieval import retrieve_for_chat, detect_category_list_intent, list_ventures_by_category, format_enumeration_answer
    from ..services.chat_service import build_context, call_chat, strip_invalid_citations
    from ..services.settings_service import get_openrouter_api_key
    from ..services.query_rewriter import condense_query
    import re as _re

    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")

    api_key = get_openrouter_api_key(db)
    if not api_key:
        raise HTTPException(503, "AI service not configured")

    # Get or create chat session
    session = None
    if body.session_id is not None:
        try:
            session = db.scalar(
                select(LPChatSession)
                .where(LPChatSession.id == int(body.session_id))
                .where(LPChatSession.lp_user_id == current_user.id)
            )
        except (ValueError, TypeError):
            pass
    if not session:
        session = LPChatSession(lp_user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    db.add(LPChatMessage(session_id=session.id, role="user", content=message))
    db.commit()

    _PORTFOLIO_RE = _re.compile(
        r"\b(portfolio\s+compan|our\s+portfolio|your\s+portfolio|portfolio\s+invest|"
        r"companies.*portfolio|portfolio.*companies|list.*portfolio|what.*invested|"
        r"which.*invested|show.*portfolio)\b", _re.I,
    )

    reply = "I could not find relevant portfolio data for this question. Please try asking about specific companies or investment themes."
    citations: list[dict] = []

    enum_term = "*" if _PORTFOLIO_RE.search(message) else detect_category_list_intent(message)
    enum_items, enum_total = [], 0
    if enum_term:
        try:
            enum_items, enum_total = list_ventures_by_category(db, enum_term, limit=50, lp_scope=True)
        except Exception as exc:
            log.warning("LP enumeration failed: %s", exc)

    if enum_term and enum_total:
        reply = format_enumeration_answer(enum_term, enum_items, enum_total, show_stage=False)
    else:
        try:
            prior = list(session.messages)[:-1]
            temp_user = User(id=current_user.id, company_id=None, role=UserRole.admin)
            search_query, focus_company = condense_query(message, prior, db)
            # If the request came from a company detail page, always focus on that company
            if not focus_company and body.company_name:
                focus_company = body.company_name
            # Expand search query with old company names so notes using former names are found
            search_query = _expand_query_with_aliases(search_query)
            chunks = retrieve_for_chat(
                query=search_query,
                user=temp_user,
                db=db,
                limit=25,
                viewer_scope="lp",
                focus_company=focus_company,
            )
            if chunks:
                context = build_context(chunks)
                # Prepend alias map so AI always uses current names
                alias_note = _build_alias_context()
                if alias_note:
                    context = alias_note + "\n\n" + context
                # Prepend deterministic scores if this is a sector/company evaluation query
                from ..services.scoring_service import detect_and_score
                score_context = detect_and_score(message, focus_company, db)
                if score_context:
                    context = score_context + "\n\n" + context
                # If from company detail page, also pin the specific company name
                if body.company_name:
                    context = (
                        f"COMPANY FOCUS MODE — STRICT: This conversation is exclusively about "
                        f"'{body.company_name}'. You must ONLY answer questions about this company. "
                        f"Do not discuss, compare, or mention any other company unless the user "
                        f"explicitly asks for a comparison. If the user asks something unrelated to "
                        f"'{body.company_name}', redirect them: 'This chat is focused on {body.company_name} — "
                        f"please use the main AI Analyst for broader questions.' "
                        f"Any references to previous names in the documents below refer to the same company — "
                        f"always use '{body.company_name}' as the name in your response.\n\n"
                        + context
                    )
                citations = [
                    {
                        "index": i + 1,
                        "company_name": c.company_name,
                        "document_title": c.document_title,
                        "excerpt": c.text[:200] + ("…" if len(c.text) > 200 else ""),
                        "source_type": c.source_type,
                    }
                    for i, c in enumerate(chunks)
                ]
                reply = call_chat(message, context, api_key, previous_messages=list(session.messages), viewer_scope="lp")
                reply = strip_invalid_citations(reply, len(citations))
        except Exception as exc:
            log.error("LP API chat error: %s", exc, exc_info=True)
            reply = "I ran into a problem. Please try again in a moment."

    db.add(LPChatMessage(
        session_id=session.id,
        role="assistant",
        content=reply,
        citations_json=json.dumps(citations) if citations else None,
    ))
    db.commit()

    return {
        "ok": True,
        "session_id": str(session.id),
        "reply": reply,
        "citations": citations,
    }


@router.get("/chat/sessions")
def api_get_chat_sessions(
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Return all chat sessions for the current user (summary only)."""
    from ..models import LPChatSession, LPChatMessage

    sessions = db.scalars(
        select(LPChatSession)
        .where(LPChatSession.lp_user_id == current_user.id)
        .order_by(LPChatSession.created_at.desc())
    ).all()

    result = []
    for s in sessions:
        messages = db.scalars(
            select(LPChatMessage)
            .where(LPChatMessage.session_id == s.id)
            .order_by(LPChatMessage.created_at.asc())
        ).all()

        first_user = next((m for m in messages if m.role == "user"), None)
        last_msg = messages[-1] if messages else None

        title = (first_user.content[:60] + "…") if first_user and len(first_user.content) > 60 else (first_user.content if first_user else "General conversation")
        last_preview = (last_msg.content[:120] + "…") if last_msg and len(last_msg.content) > 120 else (last_msg.content if last_msg else "")

        result.append({
            "id": str(s.id),
            "title": title,
            "message_count": len(messages),
            "updated_at": s.created_at.isoformat() if s.created_at else None,
            "last_message": last_preview,
        })

    return result


@router.get("/chat/sessions/{session_id}")
def api_get_chat_session(
    session_id: int,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Return a single chat session with its messages."""
    from ..models import LPChatSession, LPChatMessage

    session = db.scalar(
        select(LPChatSession)
        .where(LPChatSession.id == session_id)
        .where(LPChatSession.lp_user_id == current_user.id)
    )
    if not session:
        raise HTTPException(404, "Session not found")

    messages = db.scalars(
        select(LPChatMessage)
        .where(LPChatMessage.session_id == session.id)
        .order_by(LPChatMessage.created_at.asc())
    ).all()

    return {
        "id": str(session.id),
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "ts": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
                "citations": json.loads(m.citations_json) if m.citations_json else [],
            }
            for m in messages
        ],
    }


@router.delete("/chat/sessions/{session_id}")
def api_delete_chat_session(
    session_id: int,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a chat session and all its messages."""
    from ..models import LPChatSession, LPChatMessage
    session = db.scalar(
        select(LPChatSession)
        .where(LPChatSession.id == session_id)
        .where(LPChatSession.lp_user_id == current_user.id)
    )
    if not session:
        raise HTTPException(404, "Session not found")
    db.query(LPChatMessage).filter(LPChatMessage.session_id == session_id).delete(synchronize_session=False)
    db.delete(session)
    db.commit()
    return {"ok": True}


# ── Streaming chat endpoint ───────────────────────────────────────────────────

@router.post("/chat/stream")
async def api_chat_stream(
    body: ChatRequest,
    background_tasks: BackgroundTasks,
    current_user: LPUser = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    """
    Streaming version of chat using Server-Sent Events.
    Tokens are sent as they arrive from OpenRouter so the UI can render them immediately.
    Events: {"type":"session","session_id":"..."} | {"type":"token","text":"..."} | {"type":"done"}
    """
    from ..models import LPChatSession, LPChatMessage, User, UserRole
    from ..services.retrieval import retrieve_for_chat, detect_category_list_intent, list_ventures_by_category, format_enumeration_answer
    from ..services.chat_service import build_context, call_chat_stream, strip_invalid_citations
    from ..services.settings_service import get_openrouter_api_key
    from ..services.query_rewriter import condense_query
    from ..services.scoring_service import detect_and_score
    import re as _re

    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")

    api_key = get_openrouter_api_key(db)
    if not api_key:
        raise HTTPException(503, "AI service not configured")

    # Get or create session
    session = None
    if body.session_id is not None:
        try:
            session = db.scalar(
                select(LPChatSession)
                .where(LPChatSession.id == int(body.session_id))
                .where(LPChatSession.lp_user_id == current_user.id)
            )
        except (ValueError, TypeError):
            pass
    if not session:
        session = LPChatSession(lp_user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    db.add(LPChatMessage(session_id=session.id, role="user", content=message))
    db.commit()

    # Build context (same as regular chat)
    _PORTFOLIO_RE = _re.compile(
        r"\b(portfolio\s+compan|our\s+portfolio|your\s+portfolio|portfolio\s+invest|"
        r"companies.*portfolio|portfolio.*companies|list.*portfolio|what.*invested|"
        r"which.*invested|show.*portfolio)\b", _re.I,
    )

    context = ""
    citations: list[dict] = []
    prior = list(session.messages)[:-1]
    temp_user = User(id=current_user.id, company_id=None, role=UserRole.admin)

    enum_term = "*" if _PORTFOLIO_RE.search(message) else detect_category_list_intent(message)
    enum_items, enum_total = [], 0
    if enum_term:
        try:
            enum_items, enum_total = list_ventures_by_category(db, enum_term, limit=50, lp_scope=True)
        except Exception as exc:
            log.warning("LP enumeration failed: %s", exc)

    if enum_term and enum_total:
        # Structured enumeration — not streamed, return directly
        reply = format_enumeration_answer(enum_term, enum_items, enum_total, show_stage=False)
        db.add(LPChatMessage(session_id=session.id, role="assistant", content=reply))
        db.commit()

        async def _enum_stream():
            yield f"data: {json.dumps({'type': 'session', 'session_id': str(session.id)})}\n\n"
            # Send as tokens for consistent UI
            for word in reply.split(" "):
                yield f"data: {json.dumps({'type': 'token', 'text': word + ' '})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(_enum_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── All sync pipeline work done before streaming begins ──────────────────
    try:
        search_query, focus_company = condense_query(message, prior, db)
        if not focus_company and body.company_name:
            focus_company = body.company_name
        search_query = _expand_query_with_aliases(search_query)
        chunks = retrieve_for_chat(query=search_query, user=temp_user, db=db,
                                   limit=25, viewer_scope="lp", focus_company=focus_company)
    except Exception as exc:
        log.error("Streaming retrieval error: %s", exc, exc_info=True)
        chunks = []

    if not chunks:
        fallback = "I could not find relevant portfolio data for this question. Please try asking about specific companies or investment themes."
        db.add(LPChatMessage(session_id=session.id, role="assistant", content=fallback))
        db.commit()

        async def _fallback_stream():
            yield f"data: {json.dumps({'type': 'session', 'session_id': str(session.id)})}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'text': fallback})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(_fallback_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    context = build_context(chunks)
    alias_note = _build_alias_context()
    if alias_note:
        context = alias_note + "\n\n" + context
    score_context = detect_and_score(message, focus_company, db)
    if score_context:
        context = score_context + "\n\n" + context
    if body.company_name:
        context = (f"IMPORTANT: The company being discussed is currently named '{body.company_name}'. "
                   f"Any references to previous names refer to the same company — always use "
                   f"'{body.company_name}' as the name in your response.\n\n" + context)

    session_id_str = str(session.id)
    previous_msgs = list(session.messages)
    collected: list[str] = []

    def _save_reply():
        from ..database import SessionLocal
        bg = SessionLocal()
        try:
            full = "".join(collected)
            full = strip_invalid_citations(full, len(citations))
            bg.add(LPChatMessage(session_id=int(session_id_str), role="assistant",
                                 content=full,
                                 citations_json=json.dumps(citations) if citations else None))
            bg.commit()
        except Exception as exc:
            log.error("Stream save reply failed: %s", exc)
        finally:
            bg.close()

    background_tasks.add_task(_save_reply)

    async def _stream():
        # Send session + status immediately so the UI shows activity right away
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id_str})}\n\n"
        yield f"data: {json.dumps({'type': 'status', 'text': 'Laura is preparing your answer…'})}\n\n"
        try:
            async for token in call_chat_stream(
                message, context, api_key,
                previous_messages=previous_msgs,
                viewer_scope="lp",
            ):
                collected.append(token)
                yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
        except Exception as exc:
            log.error("Streaming LLM error: %s", exc, exc_info=True)
            err = " [Stream interrupted. Please try again.]"
            collected.append(err)
            yield f"data: {json.dumps({'type': 'token', 'text': err})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
