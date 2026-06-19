"""
LLM-based portfolio KPI extraction.

Entry point: run_portfolio_extraction(document_id, db)

Flow
----
1. Load Document + Company from DB.
2. Verify raw_text exists.
3. Build prompt via build_portfolio_extraction_messages().
4. Call OpenRouter chat completion.
5. Robust JSON parsing (strip markdown fences, validate top-level object).
6. Upsert Extraction row (status=pending_review).
7. Update Document.extraction_status → extracted.
On any failure: mark Document.extraction_status → failed, store error.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, Extraction, ExtractionJobStatus, ExtractionStatus, ReviewStatus
from ..prompts.portfolio_extraction import (
    CustomKpiFieldDef,
    build_portfolio_extraction_messages,
)
from ..services.settings_service import get_openrouter_api_key

log = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT = 120  # seconds


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove ```json … ``` or ``` … ``` wrappers that some models emit."""
    text = text.strip()
    # Match optional language tag: ```json or ```
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def _extract_json_object(text: str) -> str:
    """
    Find the first {...} block in text.
    Handles leading/trailing prose that some models add despite instructions.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response.")
    # Walk forward matching braces
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("Unbalanced braces in JSON response.")


def parse_llm_json(raw: str) -> dict:
    """
    Robustly parse a JSON object from a model response.
    Raises ValueError with a descriptive message if parsing fails.
    """
    cleaned = _strip_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try extracting just the object portion
        try:
            obj_str = _extract_json_object(cleaned)
            parsed = json.loads(obj_str)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Could not parse JSON from model response: {exc}\n"
                f"First 500 chars of response: {raw[:500]!r}"
            ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Expected a JSON object at top level, got {type(parsed).__name__}."
        )
    return parsed


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------

def _call_openrouter(messages: list[dict], api_key: str, model: str) -> str:
    """
    Call the OpenRouter chat completions endpoint synchronously.
    Returns the raw content string from the first choice.
    Raises RuntimeError on HTTP or API errors.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://portfolio-intelligence.app",
        "X-Title": "Portfolio Intelligence Platform",
    }
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }

    try:
        resp = httpx.post(
            OPENROUTER_API_URL,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"OpenRouter request timed out after {REQUEST_TIMEOUT}s.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:400]}"
        )

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {exc}\n{data}") from exc

    return content


# ---------------------------------------------------------------------------
# Upsert Extraction row
# ---------------------------------------------------------------------------

def _upsert_extraction(
    db: Session,
    *,
    document_id: int,
    company_id: int,
    raw_llm_response: str | None,
    extracted_json: str | None,
    status: ExtractionJobStatus,
    error: str | None,
) -> Extraction:
    """Create a new Extraction row (one row per run — preserves history)."""
    ext = Extraction(
        document_id=document_id,
        company_id=company_id,
        raw_llm_response=raw_llm_response,
        extracted_json=extracted_json,
        corrected_json=None,
        status=status,
        error=error,
    )
    db.add(ext)
    db.flush()  # get id before commit
    return ext


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_portfolio_extraction(document_id: int, db: Session) -> Extraction:
    """
    Run LLM KPI extraction for a document.

    Returns the created Extraction row.
    Raises ValueError for bad inputs (missing text, no API key).
    Raises RuntimeError for OpenRouter or parsing failures (also persisted to DB).
    """
    # ── 1. Load document ────────────────────────────────────────────────────
    doc: Document | None = db.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found.")

    # ── 2. Load company ─────────────────────────────────────────────────────
    from ..models import Company
    company: Company | None = db.get(Company, doc.company_id)
    company_name = company.name if company else "Unknown Company"

    # ── 3. Verify raw_text ──────────────────────────────────────────────────
    if not doc.raw_text or not doc.raw_text.strip():
        error = "Document has no extracted text. Run text extraction first."
        doc.extraction_status = ExtractionStatus.failed
        doc.extraction_error = error
        db.commit()
        raise ValueError(error)

    # ── 4. Get API key ───────────────────────────────────────────────────────
    api_key = get_openrouter_api_key(db)
    if not api_key:
        error = "OpenRouter API key is not configured. Set OPENROUTER_API_KEY or add it in Admin → Settings."
        doc.extraction_status = ExtractionStatus.failed
        doc.extraction_error = error
        db.commit()
        raise ValueError(error)

    # ── 5. Load active custom KPI fields ────────────────────────────────────
    from ..models import CompanyKpiField
    kpi_rows = list(db.scalars(
        select(CompanyKpiField).where(
            CompanyKpiField.company_id == doc.company_id,
            CompanyKpiField.is_active == True,  # noqa: E712
        ).order_by(CompanyKpiField.id)
    ).all())
    custom_fields = [
        CustomKpiFieldDef(
            field_key=f.field_key,
            field_label=f.field_label,
            field_type=f.field_type.value,
            description=f.description,
            extraction_hint=f.extraction_hint,
            is_required=f.is_required,
        )
        for f in kpi_rows
    ]

    # ── 6. Build prompt ──────────────────────────────────────────────────────
    messages = build_portfolio_extraction_messages(
        company_name=company_name,
        document_title=doc.title,
        raw_text=doc.raw_text,
        custom_kpi_fields=custom_fields or None,
    )

    # ── 7. Call OpenRouter ───────────────────────────────────────────────────
    raw_response: str | None = None
    try:
        from ..config import settings as _settings
        raw_response = _call_openrouter(messages, api_key=api_key, model=_settings.openrouter_chat_model)
    except RuntimeError as exc:
        error = str(exc)
        log.error("OpenRouter call failed for document %d: %s", document_id, error)
        _upsert_extraction(
            db,
            document_id=document_id,
            company_id=doc.company_id,
            raw_llm_response=None,
            extracted_json=None,
            status=ExtractionJobStatus.rejected,
            error=error,
        )
        doc.extraction_status = ExtractionStatus.failed
        doc.extraction_error = error
        db.commit()
        raise

    # ── 8. Parse JSON ────────────────────────────────────────────────────────
    extracted_dict: dict | None = None
    try:
        extracted_dict = parse_llm_json(raw_response)
    except ValueError as exc:
        error = str(exc)
        log.error("JSON parsing failed for document %d: %s", document_id, error)
        _upsert_extraction(
            db,
            document_id=document_id,
            company_id=doc.company_id,
            raw_llm_response=raw_response,
            extracted_json=None,
            status=ExtractionJobStatus.rejected,
            error=error,
        )
        doc.extraction_status = ExtractionStatus.failed
        doc.extraction_error = error
        db.commit()
        raise RuntimeError(error) from exc

    # ── 9. Persist extraction ────────────────────────────────────────────────
    ext = _upsert_extraction(
        db,
        document_id=document_id,
        company_id=doc.company_id,
        raw_llm_response=raw_response,
        extracted_json=json.dumps(extracted_dict, ensure_ascii=False),
        status=ExtractionJobStatus.pending_review,
        error=None,
    )

    # ── 10. Update document status ───────────────────────────────────────────
    doc.extraction_status = ExtractionStatus.extracted
    doc.review_status = ReviewStatus.pending
    doc.extraction_error = None

    db.commit()
    db.refresh(ext)
    log.info("Extraction complete for document %d → extraction %d", document_id, ext.id)
    return ext
