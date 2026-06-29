"""LP chat routes: interact with portfolio data via LLM + RAG with conversation history."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import LPUser, LPChatSession, LPChatMessage
from ..templates import templates
from .lp_auth_routes import require_lp_login

log = logging.getLogger(__name__)
router = APIRouter(prefix="/lp")

NO_CHUNKS_REPLY = (
    "I could not find relevant portfolio data for this question. "
    "Please try asking about specific companies or investment themes."
)

_EXCERPT_CHARS = 300  # chars of chunk text shown as citation excerpt


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    ctx.setdefault("request", request)
    resp = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    # No bfcache: after logout, pressing Back must re-request (and redirect to login)
    # rather than show the cached chat page.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _get_or_create_lp_chat_session(lp_user: LPUser, db: Session) -> LPChatSession:
    """Get the most recent chat session or create a new one."""
    session = db.scalar(
        select(LPChatSession)
        .where(LPChatSession.lp_user_id == lp_user.id)
        .order_by(LPChatSession.created_at.desc())
        .limit(1)
    )
    if session:
        return session

    session = LPChatSession(lp_user_id=lp_user.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _citations_from_chunks(chunks) -> list[dict]:
    """Format chunks as citations for display."""
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
        # Include CRM venture details if available
        if entry["source_type"] == "crm_venture":
            entry["crm_venture_id"] = getattr(c, "crm_venture_id", None)
            entry["crm_venture_name"] = getattr(c, "crm_venture_name", None)
            entry["attio_url"] = getattr(c, "attio_url", None)
            entry["sector"] = getattr(c, "sector", None)
            entry["themes"] = getattr(c, "themes", None)
        result.append(entry)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /lp/chat
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/chat", response_class=HTMLResponse)
def lp_chat_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser, Depends(require_lp_login)],
):
    """LP chat interface."""
    log.info(f"LP chat page loaded for user {current_user.id}")
    
    session = _get_or_create_lp_chat_session(current_user, db)
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

    return _render(request, "lp/chat.html", {
        "user": current_user,
        "session": session,
        "enriched_messages": enriched,
    })


# ─────────────────────────────────────────────────────────────────────────────
# POST /lp/chat
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/chat", response_class=HTMLResponse)
async def lp_chat_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser, Depends(require_lp_login)],
    message: Annotated[str, Form()] = "",
):
    """LP chat message submission."""
    message = message.strip()

    session = _get_or_create_lp_chat_session(current_user, db)

    if not message:
        return RedirectResponse("/lp/chat", status_code=303)

    # Save user message immediately
    db.add(LPChatMessage(session_id=session.id, role="user", content=message))
    db.commit()

    # ── Get API Key ───────────────────────────────────────────────────────────
    from ..services.settings_service import get_openrouter_api_key
    from ..services.retrieval import retrieve_for_chat
    from ..models import User, UserRole

    api_key = get_openrouter_api_key(db)

    log.info(f"LP chat: OpenRouter={'✓' if api_key else '✗'}")

    # Initialize response variables
    assistant_reply = NO_CHUNKS_REPLY
    citations: list[dict] = []

    # ── Structured enumeration shortcut (parity with admin chat) ────────────────
    # "how many / which / list <sector> companies" → deterministic, complete,
    # newest-first list from the CRM instead of semantic top-k (which only
    # returned ~3 for LPs after redaction). No embeddings / LLM needed.
    from ..services.retrieval import (
        detect_category_list_intent, list_ventures_by_category, format_enumeration_answer,
    )
    # Detect "portfolio companies" queries — always map to a full portfolio list
    import re as _re
    _PORTFOLIO_QUERY_RE = _re.compile(
        r"\b(portfolio\s+compan|our\s+portfolio|your\s+portfolio|portfolio\s+invest|"
        r"companies.*portfolio|portfolio.*companies|list.*portfolio|what.*invested|"
        r"which.*invested|show.*portfolio)\b",
        _re.I,
    )
    if _PORTFOLIO_QUERY_RE.search(message):
        enum_term = "*"  # ALL_SENTINEL — list every portfolio-stage company
    else:
        enum_term = detect_category_list_intent(message)

    enum_items: list[dict] = []
    enum_total = 0
    if enum_term:
        try:
            enum_items, enum_total = list_ventures_by_category(db, enum_term, limit=50, lp_scope=True)
        except Exception as exc:
            log.warning("LP enumeration failed for %r: %s", enum_term, exc)

    # ── Check for API key ─────────────────────────────────────────────────────
    if enum_term and enum_total:
        # Hide deal-stage labels from LPs (internal pipeline status).
        assistant_reply = format_enumeration_answer(
            enum_term, enum_items, enum_total, show_stage=False
        )
        citations = []
    elif not api_key:
        log.error("OpenRouter API key not configured")
        assistant_reply = (
            "The OpenRouter API key is not configured. "
            "Please contact the platform administrator."
        )
        citations = []
    else:
        # ── Retrieve from Knowledge Chunks (Database) ─────────────────────────
        chunks = []
        try:
            # Create a temporary admin User object for retrieval permissions
            # LP users should see all public knowledge chunks
            temp_admin_user = User(id=current_user.id, company_id=None, role=UserRole.admin)
            
            log.info(f"Retrieving knowledge chunks for LP user {current_user.id}")
            # History-aware retrieval: resolve follow-up references + company in focus.
            from ..services.query_rewriter import condense_query
            prior_history = list(session.messages)[:-1]
            search_query, focus_company = condense_query(message, prior_history, db)
            chunks = retrieve_for_chat(
                query=search_query,
                user=temp_admin_user,
                db=db,
                limit=12,             # more grounding context (LP chunks are redacted, so each carries less)
                viewer_scope="lp",   # docs + CRM with confidential fields stripped; notes/files sanitized
                focus_company=focus_company,
            )
            log.info(f"Retrieved {len(chunks)} chunks from database")
        except Exception as retrieval_exc:
            log.error(f"Retrieval error: {retrieval_exc}", exc_info=True)
            chunks = []

        # ── Check if we got any results ───────────────────────────────────────
        if not chunks:
            log.warning("No chunks retrieved from database")
            assistant_reply = NO_CHUNKS_REPLY
            citations = []
        else:
            # ── Generate LLM response with conversation history ─────────────────
            from ..services.chat_service import build_context, call_chat, strip_invalid_citations

            try:
                context = build_context(chunks)
                citations = _citations_from_chunks(chunks)
                
                # Get previous messages for context
                previous_messages = list(session.messages)
                
                log.info(f"Built context with {len(chunks)} chunks and {len(previous_messages)} previous messages")
                
                # Call LLM with conversation history + LP guardrail
                assistant_reply = call_chat(
                    message, context, api_key,
                    previous_messages=previous_messages,
                    viewer_scope="lp",   # defense-in-depth prompt instruction
                )
                assistant_reply = strip_invalid_citations(assistant_reply, len(citations))
                log.info(f"Generated response: {assistant_reply[:80]}...")

            except Exception as llm_exc:
                log.error(f"LLM error: {llm_exc}", exc_info=True)
                assistant_reply = (
                    "I ran into a problem generating a response. Please try again in a moment."
                )
                citations = []

    # Save assistant message
    db.add(LPChatMessage(
        session_id=session.id,
        role="assistant",
        content=assistant_reply,
        citations_json=json.dumps(citations) if citations else None,
    ))
    db.commit()

    log.info(f"LP chat completed for user {current_user.id}")
    return RedirectResponse("/lp/chat", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# POST /lp/chat/clear
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/chat/clear")
def lp_chat_clear(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[LPUser, Depends(require_lp_login)],
):
    """Clear LP chat history (create new session)."""
    db.add(LPChatSession(lp_user_id=current_user.id))
    db.commit()
    log.info(f"Cleared chat history for user {current_user.id}")
    return RedirectResponse("/lp/chat", status_code=303)