
"""Chat routes: GET /chat (history + form), POST /chat (submit question)."""
from __future__ import annotations
 
import json
import logging
from typing import Annotated
 
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
 
from ..auth import require_login
from ..database import get_db
from ..models import ChatMessage, ChatSession, Company, User, UserRole
from ..templates import templates
 
log = logging.getLogger(__name__)
router = APIRouter()
 
NO_CHUNKS_REPLY = (
    "I could not find relevant approved portfolio data for this question. "
    "Make sure documents have been uploaded, extracted, and approved."
)
 
_EXCERPT_CHARS = 300  # chars of chunk text shown as citation excerpt
 
 
def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)
 
 
def _get_or_create_session(user: User, company_id: int | None, db: Session) -> ChatSession:
    """
    Return the user's most recent chat session (creating one only if none exists),
    so the conversation thread — and its history — is continuous across turns.

    The company filter no longer forks the session: it is applied per-question at
    retrieval time, and we just keep the session's company_id in sync for display.
    A fresh thread is started explicitly via "Clear chat" (POST /chat/clear).
    """
    session = db.scalar(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.created_at.desc())
        .limit(1)
    )

    if session is None:
        desired_cid = company_id if user.role == UserRole.admin else user.company_id
        session = ChatSession(user_id=user.id, company_id=desired_cid)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    # Reuse the same conversation. Keep the admin company filter current for display
    # only (does NOT start a new thread, so history is preserved on follow-ups).
    if user.role == UserRole.admin and company_id is not None and session.company_id != company_id:
        session.company_id = company_id
        db.commit()

    return session
 
 
def _citations_from_chunks(chunks) -> list[dict]:
    result = []
    for i, c in enumerate(chunks, start=1):
        entry: dict = {
            "index": i,
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "extraction_id": c.extraction_id,
            "company_id": c.company_id,
            "company_name": c.company_name,
            "document_title": c.document_title,
            "excerpt": c.text[:_EXCERPT_CHARS] + ("…" if len(c.text) > _EXCERPT_CHARS else ""),
            "score": c.score,
            "source_type": getattr(c, "source_type", "portfolio"),
        }
        if entry["source_type"] == "crm_venture":
            entry["crm_venture_id"] = getattr(c, "crm_venture_id", None)
            entry["crm_venture_name"] = getattr(c, "crm_venture_name", None)
            entry["attio_url"] = getattr(c, "attio_url", None)
            entry["sector"] = getattr(c, "sector", None)
            entry["themes"] = getattr(c, "themes", None)
        result.append(entry)
    return result
 
 
def _all_companies(db: Session) -> list[Company]:
    return list(db.scalars(select(Company).order_by(Company.name)).all())
 
 
# ---------------------------------------------------------------------------
# GET /chat
# ---------------------------------------------------------------------------
 
@router.get("/chat", response_class=HTMLResponse)
def chat_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    is_admin = current_user.role == UserRole.admin
    companies = _all_companies(db) if is_admin else []
 
    session = _get_or_create_session(current_user, None, db)
    messages = list(session.messages)
 
    enriched = []
    for msg in messages:
        citations = []
        if msg.role == "assistant" and msg.citations_json:
            try:
                citations = json.loads(msg.citations_json)
            except Exception:
                pass
        enriched.append({"msg": msg, "citations": citations})
 
    # Resolve company name for user scope display
    user_company = None
    if current_user.company_id:
        user_company = db.get(Company, current_user.company_id)
 
    return _render(request, "chat/chat.html", {
        "user": current_user,
        "session": session,
        "enriched_messages": enriched,
        "companies": companies,
        "selected_company_id": session.company_id,
        "user_company": user_company,
    })
 
 
# ---------------------------------------------------------------------------
# POST /chat
# ---------------------------------------------------------------------------
 
@router.post("/chat", response_class=HTMLResponse)
async def chat_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
    message: Annotated[str, Form()] = "",
    company_id: Annotated[str, Form()] = "",
):
    message = message.strip()
    is_admin = current_user.role == UserRole.admin
 
    # Parse optional company filter (admin only)
    filter_company_id: int | None = None
    if is_admin and company_id.strip():
        try:
            filter_company_id = int(company_id)
        except ValueError:
            pass
 
    session = _get_or_create_session(current_user, filter_company_id, db)
 
    if not message:
        return RedirectResponse("/chat", status_code=303)
 
    # Save user turn immediately
    db.add(ChatMessage(session_id=session.id, role="user", content=message))
    db.commit()
 
    # ── Retrieve from Knowledge Chunks ──────────────────────────────────────
    from ..services.retrieval import retrieve_for_chat
    from ..services.settings_service import get_openrouter_api_key
 
    api_key = get_openrouter_api_key(db)
 
    # ── Structured enumeration shortcut ─────────────────────────────────────
    # "list / which / how many <sector> companies" is an enumeration, not a
    # similarity question. Semantic top-k only returns ~8 chunks (a handful of
    # companies) and never the most recent. For these, answer deterministically
    # and completely straight from the CRM, newest-first. (Admins only — we do
    # not enumerate the pipeline for LP / company users.)
    from ..services.retrieval import (
        detect_category_list_intent, list_ventures_by_category, format_enumeration_answer,
    )
    enum_term = detect_category_list_intent(message) if is_admin else None
    enum_items: list[dict] = []
    enum_total = 0
    if enum_term:
        try:
            enum_items, enum_total = list_ventures_by_category(db, enum_term, limit=50)
        except Exception as exc:
            log.warning("Enumeration failed for %r: %s", enum_term, exc)

    if enum_term and enum_total:
        assistant_reply = format_enumeration_answer(enum_term, enum_items, enum_total)
        citations: list[dict] = []
    elif not api_key:
        assistant_reply = (
            "The OpenRouter API key is not configured. "
            "Ask an admin to add it under Admin → Settings."
        )
        citations = []
    else:
        retrieval_filters = {}
        if filter_company_id:
            retrieval_filters["company_id"] = filter_company_id

        # Role-based access scope: admins see everything; company users get
        # their own docs in full + general (redacted) info about other companies.
        scope = "admin" if is_admin else "company_user"

        # Conversation memory: prior turns (exclude the question we just saved).
        history = list(session.messages)[:-1]
        # History-aware retrieval: rewrite follow-ups ("their team structure") into a
        # standalone query and resolve the company in focus, so we fetch the right
        # company deterministically rather than whatever globally matches.
        from ..services.query_rewriter import condense_query
        search_query, focus_company = condense_query(message, history, db)

        try:
            # Merge approved portfolio docs + CRM knowledge (role-gated)
            chunks = retrieve_for_chat(
                query=search_query,
                user=current_user,
                db=db,
                filters=retrieval_filters,
                limit=8,
                viewer_scope=scope,
                focus_company=focus_company,
            )
        except Exception as exc:
            log.warning("Retrieval error for user %d: %s", current_user.id, exc)
            chunks = []

        if not chunks:
            assistant_reply = NO_CHUNKS_REPLY
            citations = []
        else:
            from ..services.chat_service import build_context, call_chat, strip_invalid_citations

            context = build_context(chunks)
            citations = _citations_from_chunks(chunks)
            try:
                assistant_reply = call_chat(
                    message, context, api_key,
                    previous_messages=history, viewer_scope=scope,
                )
                assistant_reply = strip_invalid_citations(assistant_reply, len(citations))
            except Exception as exc:
                log.warning("Chat LLM error for user %d: %s", current_user.id, exc)
                assistant_reply = (
                    "I ran into a problem generating a response. Please try again in a moment."
                )
 
    # Save assistant turn
    db.add(ChatMessage(
        session_id=session.id,
        role="assistant",
        content=assistant_reply,
        citations_json=json.dumps(citations) if citations else None,
    ))
    db.commit()
 
    return RedirectResponse("/chat", status_code=303)
 
 
# ---------------------------------------------------------------------------
# POST /chat/clear
# ---------------------------------------------------------------------------
 
@router.post("/chat/clear")
def chat_clear(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_login)],
):
    # Create a fresh session (old messages stay in DB but won't load)
    db.add(ChatSession(
        user_id=current_user.id,
        company_id=current_user.company_id if current_user.role != UserRole.admin else None,
    ))
    db.commit()
    return RedirectResponse("/chat", status_code=303)