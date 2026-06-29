"""
lp_api_routes.py — JSON REST API for the React LP frontend.

All endpoints return JSON (not HTML). Auth uses Bearer tokens so the React
app can run on a different domain from the FastAPI backend.

Endpoints:
  POST /api/lp/register      Create account, send OTP to email
  POST /api/lp/verify-otp    Verify OTP → return bearer token
  POST /api/lp/login         Email + password → return bearer token
  GET  /api/lp/me            Get current user profile
  PUT  /api/lp/me            Update profile / complete onboarding
  POST /api/lp/chat          Send a message, get AI response
  POST /api/lp/logout        Invalidate session
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import secrets
import smtplib
import string
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password, _token_hash
from ..config import settings
from ..database import get_db
from ..models import AppSetting, LPUser, LPUserSession

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lp")


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


# ── OTP helpers ───────────────────────────────────────────────────────────────

_OTP_TTL_MINUTES = 10


def _otp_key(email: str) -> str:
    return f"otp:{email.lower().strip()}"


def _store_otp(email: str, code: str, db: Session) -> None:
    expiry = (datetime.utcnow() + timedelta(minutes=_OTP_TTL_MINUTES)).isoformat()
    value = f"{code}:{expiry}"
    key = _otp_key(email)
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def _verify_otp(email: str, code: str, db: Session) -> bool:
    key = _otp_key(email)
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    if not row or not row.value:
        return False
    parts = row.value.split(":")
    if len(parts) < 2:
        return False
    stored_code = parts[0]
    expiry_str = ":".join(parts[1:])
    try:
        expiry = datetime.fromisoformat(expiry_str)
    except ValueError:
        return False
    if datetime.utcnow() > expiry:
        return False
    if stored_code != code.strip():
        return False
    # Consume the OTP
    db.delete(row)
    db.commit()
    return True


def _send_otp_email(to_email: str, code: str) -> bool:
    """Send OTP via SMTP. Returns True if sent, False if SMTP not configured."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    if not smtp_host:
        log.info("SMTP not configured — OTP for %s: %s", to_email, code)
        return False
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASSWORD", "")
        smtp_from = os.environ.get("SMTP_FROM", smtp_user)

        body = f"""Your Merantix Capital LP Portal verification code is:

  {code}

This code expires in {_OTP_TTL_MINUTES} minutes. If you didn't request this, ignore this email.

— Merantix Capital"""
        msg = MIMEText(body)
        msg["Subject"] = f"{code} — Your Merantix LP Portal verification code"
        msg["From"] = smtp_from
        msg["To"] = to_email

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.ehlo()
            if smtp_port != 25:
                s.starttls()
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_from, [to_email], msg.as_string())
        log.info("OTP email sent to %s", to_email)
        return True
    except Exception as exc:
        log.error("Failed to send OTP email to %s: %s", to_email, exc)
        return False


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
        "avatar": None,  # not stored server-side in current schema
    }


# ── Request/response models ───────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    email: str
    company: str = ""
    password: str


class VerifyOtpRequest(BaseModel):
    email: str
    code: str


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
    session_id: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register")
def api_register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new LP account and send OTP to email."""
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

    # Create user (not yet active — pending OTP)
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

    # Generate + store OTP
    code = "".join(random.choices(string.digits, k=6))
    _store_otp(email, code, db)
    email_sent = _send_otp_email(email, code)

    return {
        "ok": True,
        "email_sent": email_sent,
        # Return code only if email not sent (dev/beta mode)
        "dev_code": code if not email_sent else None,
        "message": f"Verification code sent to {email}" if email_sent else f"SMTP not configured — code is {code}",
    }


@router.post("/verify-otp")
def api_verify_otp(body: VerifyOtpRequest, db: Session = Depends(get_db)):
    """Verify OTP → return bearer token."""
    email = body.email.strip().lower()
    user = db.scalar(select(LPUser).where(LPUser.email == email))
    if not user:
        raise HTTPException(400, "No account found with this email")

    if not _verify_otp(email, body.code, db):
        raise HTTPException(400, "Invalid or expired verification code")

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
        first = body.first_name or current_user.name.split()[0]
        last = body.last_name or (" ".join(current_user.name.split()[1:]) if len(current_user.name.split()) > 1 else "")
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
    if body.session_id:
        session = db.scalar(
            select(LPChatSession)
            .where(LPChatSession.id == int(body.session_id))
            .where(LPChatSession.lp_user_id == current_user.id)
        )
    if not session:
        session = LPChatSession(lp_user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    # Save user message
    db.add(LPChatMessage(session_id=session.id, role="user", content=message))
    db.commit()

    # Portfolio query detection
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

    # Save assistant message
    db.add(LPChatMessage(
        session_id=session.id,
        role="assistant",
        content=reply,
        citations_json=json.dumps(citations) if citations else None,
    ))
    db.commit()

    return {
        "ok": True,
        "session_id": session.id,
        "reply": reply,
        "citations": citations,
    }


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


@router.post("/resend-otp")
def api_resend_otp(body: VerifyOtpRequest, db: Session = Depends(get_db)):
    """Resend OTP to the given email (only body.email is used)."""
    email = body.email.strip().lower()
    user = db.scalar(select(LPUser).where(LPUser.email == email))
    if not user:
        raise HTTPException(400, "No account found with this email")
    code = "".join(random.choices(string.digits, k=6))
    _store_otp(email, code, db)
    email_sent = _send_otp_email(email, code)
    return {
        "ok": True,
        "email_sent": email_sent,
        "dev_code": code if not email_sent else None,
    }
