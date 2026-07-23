"""
CRM ventures knowledge indexer.

Reads raw Attio JSON stored on CrmVenture rows, builds readable text,
embeds it, and stores the results in knowledge_sources + knowledge_chunks.

Entry points
------------
index_crm_venture(crm_venture_id, db) -> int   # number of chunks created
index_all_crm_ventures(db) -> dict             # {"indexed": N, "errors": M}
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Marker the sanitizer returns when a note/file has no shareable content left.
NO_SHAREABLE_CONTENT = "[No shareable content]"

_SANITIZE_SYSTEM = (
    "You sanitize internal venture-capital CRM notes so they can safely be shown "
    "to external or limited users (portfolio-company users and LPs). Rewrite the "
    "text, KEEPING as much substantive, useful, specific detail as possible: what "
    "the company does, its product/technology, sector and categorization, market "
    "context, traction in qualitative terms, and any qualitative analysis or "
    "investment-thesis reasoning (why the company is notable, how it is assessed, "
    "the reasoning behind its classification). Preserve the depth and specificity "
    "of that content — do NOT generalize, shorten, or water it down.\n\n"
    "REMOVE COMPLETELY (never include, paraphrase, or hint at): monetary figures or "
    "amounts of any kind (revenue, ARR, MRR, burn, runway, cash, valuation, round/"
    "funding/investment amounts, ticket sizes), ownership/equity/cap-table details, "
    "deal stage, probability, pipeline status, specific investment decisions, and "
    "personal information about individuals (names of people, email addresses, phone "
    "numbers, personal contact details).\n\n"
    "Do not invent anything not present in the source. If, after removing the above, "
    f"no useful shareable content remains, output exactly: {NO_SHAREABLE_CONTENT}\n"
    "Output ONLY the rewritten text, with no preamble or explanation."
)


def sanitize_text_llm(raw_text: str, api_key: str) -> str | None:
    """
    Return a non-admin-safe rewrite of *raw_text* (financials/personal/deal info
    removed, substance preserved), or None on failure.

    Fails closed: on any error returns None, so the caller stores no sanitized
    copy and non-admins will simply not see that note/file (never the raw text).
    """
    import httpx

    if not raw_text or not raw_text.strip() or not api_key:
        return None

    from ..config import settings as _cfg

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://portfolio-intelligence.app",
        "X-Title": "Portfolio Intelligence Platform",
    }
    payload = {
        "model": _cfg.openrouter_chat_model,
        "messages": [
            {"role": "system", "content": _SANITIZE_SYSTEM},
            {"role": "user", "content": raw_text[:8000]},
        ],
        "temperature": 0.0,
        "max_tokens": 1500,
    }
    try:
        resp = httpx.post(_OPENROUTER_CHAT_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            log.warning("sanitize_text_llm: OpenRouter HTTP %s", resp.status_code)
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        content = (content or "").strip()
        return content or None
    except Exception as exc:
        log.warning("sanitize_text_llm failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Attio field slug → display label mapping
# ---------------------------------------------------------------------------

_FIELD_LABELS: dict[str, str] = {
    "name": "Company",
    "domains": "Website/domain",
    "description": "Description",
    "categories": "Categories",
    "linkedin": "LinkedIn",
    "estimated_arr_usd": "Estimated ARR (USD)",
    "funding_raised_usd": "Funding raised (USD)",
    "foundation_date": "Founded",
    "employee_range": "Employees",
    "first_interaction": "First interaction",
    "last_interaction": "Last interaction",
    "next_interaction": "Next interaction",
    "primary_location": "Location",
    "owner": "Owner",
    "probability": "Probability",
    "investment_stage": "Investment stage",
    "investment_industry": "Industry",
    "sectors": "Sectors",
    "multi_sectors": "Sectors",
    "ai_checkbox": "AI company",
    "history": "History / notes",
    # generic fallbacks already used by normalizer
    "stage": "Stage",
    "sector": "Sector",
    "status": "Status",
    "source": "Source",
}

# Manufacturing / industrial-AI detection keywords
_MANUFACTURING_KEYWORDS = {
    "manufacturing", "industrial", "factory", "shopfloor", "shop floor",
    "production", "robotics", "automation", "quality inspection",
    "predictive maintenance", "computer vision", "mes", "erp",
    "supply chain", "assembly line", "machine vision", "defect detection",
    "iot", "industry 4.0", "smart factory", "oem",
}

_THEME_KEYWORDS: dict[str, set[str]] = {
    "healthcare-ai": {
        "health", "medical", "clinical", "hospital", "pharma", "biotech",
        "diagnostic", "drug", "therapy", "patient", "ehr", "genomic",
    },
    "cybersecurity": {
        "security", "cyber", "threat", "vulnerability", "firewall",
        "soc", "siem", "endpoint", "zero-trust", "compliance",
    },
    "fintech": {
        "fintech", "finance", "banking", "payment", "insurance", "lending",
        "wealth", "credit", "blockchain", "defi", "regtech",
    },
    "legaltech": {
        "legal", "law firm", "contract", "compliance", "litigation", "attorney",
        "regulatory",
    },
    "climate-tech": {
        "climate", "carbon", "sustainability", "renewable", "energy transition",
        "net zero", "emission", "cleantech", "green",
    },
    "edtech": {
        "education", "learning", "school", "university", "tutoring", "edtech",
        "e-learning", "curriculum",
    },
    "hrtech": {
        "hr", "human resources", "talent", "recruiting", "workforce", "payroll",
        "people analytics",
    },
    "proptech": {
        "real estate", "proptech", "property", "construction", "building",
        "smart home",
    },
}


# ---------------------------------------------------------------------------
# Low-level: extract a single Attio value from raw JSON
# ---------------------------------------------------------------------------

def _pick_value(values: list[dict]) -> Any:
    """Same logic as attio_client._pick_value — duplicated to avoid circular imports."""
    if not isinstance(values, list) or not values:
        return None
    for entry in values:
        if not isinstance(entry, dict):
            continue
        if "domain" in entry and entry["domain"]:
            return entry["domain"]
        if "email_address" in entry and entry["email_address"]:
            return entry["email_address"]
        if "first_name" in entry or "last_name" in entry:
            parts = [entry.get("first_name") or "", entry.get("last_name") or ""]
            name = " ".join(p for p in parts if p).strip()
            if name:
                return name
        if "name" in entry and isinstance(entry["name"], str) and entry["name"]:
            return entry["name"]
        if "option" in entry:
            opt = entry["option"]
            if isinstance(opt, dict):
                return opt.get("title") or opt.get("value") or opt.get("api_slug")
            if opt is not None:
                return str(opt)
        if "status" in entry:
            s = entry["status"]
            if isinstance(s, dict):
                return s.get("title") or s.get("api_slug")
            if s is not None:
                return str(s)
        if "currency_value" in entry and entry["currency_value"] is not None:
            return str(entry["currency_value"])
        if "resolved_name" in entry and entry["resolved_name"]:
            return entry["resolved_name"]
        v = entry.get("value")
        if v is not None and v != "":
            return v
    return None


def extract_attio_field(raw_json: str | None, slug: str) -> Any:
    """
    Tolerantly extract a field from a stored Attio JSON blob (entry or record).
    Returns None if slug is absent or blob is invalid.
    """
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except Exception:
        return None

    # Try entry_values first (list-entry blob), then values (record blob)
    for key in ("entry_values", "values"):
        container = data.get(key)
        if isinstance(container, dict):
            raw_values = container.get(slug)
            if raw_values is not None:
                v = _pick_value(raw_values if isinstance(raw_values, list) else [raw_values])
                if v is not None:
                    return v

    # Flat top-level fallback (legacy / object sync blob)
    v = data.get(slug)
    if v is not None:
        return v

    return None


def normalize_attio_value(value: Any) -> str | None:
    """Convert an extracted Attio value to a clean readable string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Text builder
# ---------------------------------------------------------------------------

def build_crm_venture_text(venture) -> str:
    """
    Build a readable text block for a CrmVenture row.

    Priority: raw_entry_json > raw_record_json > raw_attio_json > normalised ORM fields.
    """
    lines: list[str] = []

    def _add(label: str, value: Any) -> None:
        v = normalize_attio_value(value)
        if v:
            lines.append(f"{label}: {v}")

    # ── Prefer data extracted directly from raw blobs ────────────────────────
    raw_blobs = [venture.raw_entry_json, venture.raw_record_json, venture.raw_attio_json]

    def _field(*slugs: str) -> Any:
        for slug in slugs:
            for blob in raw_blobs:
                v = extract_attio_field(blob, slug)
                if v is not None:
                    return v
        return None

    # Core identity
    _add("Company", _field("name", "company_name") or venture.name)
    _add("Founders", getattr(venture, "founders", None))
    _add("Website/domain", _field("domains", "website", "domain", "url") or venture.website)
    _add("Description", _field("description", "about", "bio") or venture.description)
    _add("Categories", _field("categories", "category"))
    _add("LinkedIn", _field("linkedin", "linkedin_url"))

    # Financials
    _add("Estimated ARR (USD)", _field("estimated_arr_usd", "arr", "estimated_arr"))
    _add("Funding raised (USD)", _field("funding_raised_usd", "total_funding", "funding_raised"))

    # Company info
    _add("Founded", _field("foundation_date", "founded", "founding_date"))
    _add("Employees", _field("employee_range", "headcount", "employees", "team_size"))
    _add("Location", _field("primary_location", "location", "city", "country"))

    # Pipeline / sector
    _add("Investment stage", _field("investment_stage", "stage", "deal_stage") or venture.stage)
    _add("Industry", _field("investment_industry", "industry", "sector") or venture.sector)
    _add("Sectors", _field("sectors", "multi_sectors"))
    _add("AI company", _field("ai_checkbox", "is_ai"))

    # Relationship
    _add("Owner", _field("owner", "assigned_to") or venture.owner)
    _add("Source", _field("source", "lead_source") or venture.source)
    _add("Status", _field("status", "company_status") or venture.status)
    _add("Probability", _field("probability"))
    _add("First interaction", _field("first_interaction"))
    _add("Last interaction", _field("last_interaction"))
    _add("Next interaction", _field("next_interaction"))

    # Notes / history
    _add("History / notes", _field("history", "notes", "note"))

    return "\n".join(lines)




def build_crm_venture_chunks(venture) -> list[tuple[str, str]]:
    """
    Split a CRM venture into 3 focused text chunks for more precise retrieval.
    Returns list of (chunk_label, text) tuples.

    Chunk A — Overview:  what the company does (drives "what does X do?" queries)
    Chunk B — Market:    sector, industry, themes (drives sector/market queries)
    Chunk C — Profile:   founding info, team size, location (drives factual queries)
    """
    raw_blobs = [venture.raw_entry_json, venture.raw_record_json, venture.raw_attio_json]

    def _field(*slugs):
        from .knowledge_indexer import extract_attio_field, normalize_attio_value
        for slug in slugs:
            for blob in raw_blobs:
                v = extract_attio_field(blob, slug)
                if v is not None:
                    nv = normalize_attio_value(v)
                    if nv:
                        return nv
        return None

    def _norm(val):
        from .knowledge_indexer import normalize_attio_value
        v = normalize_attio_value(val)
        return v or ""

    name = _field("name", "company_name") or venture.name or ""
    prefix = f"Company: {name}"

    chunks: list[tuple[str, str]] = []

    # ── Chunk A: Overview ─────────────────────────────────────────────────
    lines_a: list[str] = [prefix]
    founders = getattr(venture, "founders", None)
    website = _field("domains", "website", "domain", "url") or venture.website
    desc = _field("description", "about", "bio") or venture.description
    cats = _field("categories", "category")
    ai = _field("ai_checkbox", "is_ai")
    linkedin = _field("linkedin", "linkedin_url")
    if founders:  lines_a.append(f"Founders: {founders}")
    if website:   lines_a.append(f"Website: {website}")
    if desc:      lines_a.append(f"Description: {desc}")
    if cats:      lines_a.append(f"Categories: {cats}")
    if ai:        lines_a.append(f"AI company: {ai}")
    if linkedin:  lines_a.append(f"LinkedIn: {linkedin}")
    if len(lines_a) > 1:
        chunks.append(("overview", "\n".join(lines_a)))

    # ── Chunk B: Market & sector ──────────────────────────────────────────
    lines_b: list[str] = [prefix]
    sector = _field("investment_industry", "industry", "sector") or venture.sector
    sectors = _field("sectors", "multi_sectors")
    stage = _field("investment_stage", "stage", "deal_stage") or venture.stage
    if sector:   lines_b.append(f"Sector: {sector}")
    if sectors:  lines_b.append(f"Sectors: {sectors}")
    if stage:    lines_b.append(f"Stage: {stage}")
    if len(lines_b) > 1:
        chunks.append(("market", "\n".join(lines_b)))

    # ── Chunk C: Company profile ──────────────────────────────────────────
    lines_c: list[str] = [prefix]
    founded = _field("foundation_date", "founded", "founding_date")
    employees = _field("employee_range", "headcount", "employees", "team_size")
    location = _field("primary_location", "location", "city", "country")
    history = _field("history", "notes", "note")
    if founded:   lines_c.append(f"Founded: {founded}")
    if employees: lines_c.append(f"Employees: {employees}")
    if location:  lines_c.append(f"Location: {location}")
    if history:   lines_c.append(f"History: {history}")
    if len(lines_c) > 1:
        chunks.append(("profile", "\n".join(lines_c)))

    # Fall back to single chunk if nothing split
    if not chunks:
        from .knowledge_indexer import build_crm_venture_text
        fallback = build_crm_venture_text(venture)
        if fallback.strip():
            chunks.append(("full", fallback))

    return chunks

# ---------------------------------------------------------------------------
# Theme inference
# ---------------------------------------------------------------------------

def infer_basic_themes(text: str, sector: str | None) -> list[str]:
    """Keyword-based theme tags from text + sector string."""
    lowered = (text + " " + (sector or "")).lower()
    themes: list[str] = []

    # Manufacturing / industrial-AI detection
    mfg_hits = sum(1 for kw in _MANUFACTURING_KEYWORDS if kw in lowered)
    if mfg_hits >= 2:
        themes.append("manufacturing")
        themes.append("industrial-ai")

    # Other domain themes
    for theme, keywords in _THEME_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            themes.append(theme)

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for t in themes:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Core indexer
# ---------------------------------------------------------------------------

def index_crm_venture(crm_venture_id: int, db: Session) -> int:
    """
    Embed and store focused knowledge chunks for one CRM venture.
    Creates up to 3 chunks (overview, market, profile) for precise retrieval.
    Deletes any existing chunks/source for this venture first (re-index).
    Returns the number of chunks created.
    """
    from ..models import CrmVenture, KnowledgeChunk, KnowledgeSource
    from .embeddings import embed_texts
    from .settings_service import get_openrouter_api_key

    venture = db.get(CrmVenture, crm_venture_id)
    if not venture:
        log.warning("index_crm_venture: venture %d not found", crm_venture_id)
        return 0

    api_key = get_openrouter_api_key(db)
    if not api_key:
        log.warning("index_crm_venture: OpenRouter API key not configured")
        return 0

    # ── Build focused chunks ───────────────────────────────────────────────
    focused_chunks = build_crm_venture_chunks(venture)
    if not focused_chunks:
        log.debug("index_crm_venture: venture %d has no text to index", crm_venture_id)
        return 0

    sector = venture.sector

    # ── Delete old source + chunks ─────────────────────────────────────────
    old_source = db.scalar(
        select(KnowledgeSource).where(
            KnowledgeSource.source_type == "crm_venture",
            KnowledgeSource.source_id == crm_venture_id,
        )
    )
    if old_source:
        db.delete(old_source)
        db.flush()

    # ── Batch embed all chunks ─────────────────────────────────────────────
    texts = [text for _, text in focused_chunks]
    try:
        vectors = embed_texts(texts, api_key)
    except Exception as exc:
        log.warning("index_crm_venture: embedding failed for venture %d: %s", crm_venture_id, exc)
        return 0

    if not vectors or len(vectors) != len(texts):
        log.warning("index_crm_venture: embedding mismatch for venture %d", crm_venture_id)
        return 0

    # ── Persist ────────────────────────────────────────────────────────────
    source = KnowledgeSource(
        source_type="crm_venture",
        source_id=crm_venture_id,
        crm_venture_id=crm_venture_id,
        title=venture.name or f"CRM venture {crm_venture_id}",
        visibility="admin",
        approved=True,
    )
    db.add(source)
    db.flush()

    created = 0
    for (label, text_body), vec in zip(focused_chunks, vectors):
        themes = infer_basic_themes(text_body, sector)
        chunk = KnowledgeChunk(
            knowledge_source_id=source.id,
            crm_venture_id=crm_venture_id,
            source_type="crm_venture",
            source_id=crm_venture_id,
            text=text_body,
            embedding=json.dumps(vec),
            sector=sector,
            themes_json=json.dumps(themes) if themes else None,
            visibility="admin",
            approved=True,
        )
        db.add(chunk)
        db.flush()
        try:
            from .vector_store import set_row_vector
            set_row_vector(db, "knowledge_chunks", chunk.id, vec)
        except Exception:
            pass
        created += 1

    db.commit()
    log.debug(
        "index_crm_venture: indexed venture %d (%s) with %d focused chunks",
        crm_venture_id, venture.name, created,
    )
    return created


def index_all_crm_ventures(db: Session) -> dict:
    """
    Re-index every CRM venture.  Returns {"indexed": N, "errors": M}.
    """
    from ..models import CrmVenture
    from sqlalchemy.orm import undefer

    ventures = list(db.scalars(
        select(CrmVenture).options(
            undefer(CrmVenture.raw_entry_json),
            undefer(CrmVenture.raw_record_json),
            undefer(CrmVenture.raw_attio_json),
        )
    ).all())
    indexed = errors = 0

    for v in ventures:
        try:
            n = index_crm_venture(v.id, db)
            indexed += n
        except Exception as exc:
            log.error("index_all_crm_ventures: error on venture %d: %s", v.id, exc)
            errors += 1

    log.info(
        "index_all_crm_ventures: %d indexed, %d errors (total %d ventures)",
        indexed, errors, len(ventures),
    )
    return {"indexed": indexed, "errors": errors}


# ---------------------------------------------------------------------------
# Note text builder
# ---------------------------------------------------------------------------

def build_crm_note_text(note) -> str:
    """Build readable text for a CrmNote, prefixed with company context."""
    lines: list[str] = []

    # Company context
    if note.crm_venture:
        v = note.crm_venture
        if v.name:
            lines.append(f"Company: {v.name}")
        if v.website:
            lines.append(f"Website: {v.website}")
        if v.sector:
            lines.append(f"Sector: {v.sector}")
        if v.stage:
            lines.append(f"Stage: {v.stage}")

    # Note metadata
    if note.title:
        lines.append(f"Note title: {note.title}")
    if note.created_at_attio:
        lines.append(f"Date: {note.created_at_attio.strftime('%d %b %Y')}")
    if note.created_by:
        lines.append(f"Author: {note.created_by}")

    # Body
    if note.content_text and note.content_text.strip():
        lines.append("")
        lines.append(note.content_text.strip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Note indexer
# ---------------------------------------------------------------------------

def index_crm_note(crm_note_id: int, db) -> int:
    """
    Embed and store a knowledge chunk for one CrmNote.
    visibility=admin, approved=False (requires manual approval for chat).
    Returns 1 on success, 0 on skip/failure.
    """
    import json as _json
    from ..models import CrmNote, KnowledgeChunk, KnowledgeSource
    from .embeddings import embed_texts, chunk_text
    from .settings_service import get_openrouter_api_key
    from .vector_store import set_row_vector

    note = db.get(CrmNote, crm_note_id)
    if not note:
        return 0

    # Load venture for context
    if note.crm_venture_id and not note.crm_venture:
        from ..models import CrmVenture
        from sqlalchemy import select as _sel
        note.crm_venture = db.scalar(_sel(CrmVenture).where(CrmVenture.id == note.crm_venture_id))

    api_key = get_openrouter_api_key(db)
    if not api_key:
        return 0

    venture = note.crm_venture
    sector = venture.sector if venture else None

    # Build a short company/note header that is prepended to EVERY chunk, so each
    # window stays self-describing and keyword-matchable (company name, note title).
    header_lines: list[str] = []
    if venture:
        if venture.name:
            header_lines.append(f"Company: {venture.name}")
        if venture.website:
            header_lines.append(f"Website: {venture.website}")
        if venture.sector:
            header_lines.append(f"Sector: {venture.sector}")
        if venture.stage:
            header_lines.append(f"Stage: {venture.stage}")
    if note.title:
        header_lines.append(f"Note: {note.title}")
    if note.created_at_attio:
        header_lines.append(f"Date: {note.created_at_attio.strftime('%d %b %Y')}")
    header = "\n".join(header_lines)

    body = (note.content_text or "").strip()
    # Split large notes into overlapping windows so specific passages (thesis,
    # team, risks) become their own retrievable chunks instead of one diluted blob.
    windows = chunk_text(body) if body else []
    if not windows:
        windows = [header] if header else []
    if not windows:
        return 0

    # Build every window's body first (header + window text). These are the
    # exact strings we embed, and they also let us detect "nothing changed".
    chunk_bodies = [
        (f"{header}\n\n{w}".strip() if header else w) for w in windows
    ]

    from sqlalchemy import select as _sel, delete as _del
    old_source = db.scalar(
        _sel(KnowledgeSource).where(
            KnowledgeSource.source_type == "crm_note",
            KnowledgeSource.source_id == crm_note_id,
        )
    )

    # ── Skip-unchanged ───────────────────────────────────────────────────────
    # If this note is already indexed with identical chunk text, do nothing —
    # no embedding and no sanitization API calls. This is what makes the
    # scheduled re-index cheap: only new or edited notes ever hit the API.
    if old_source:
        existing_texts = list(db.scalars(
            _sel(KnowledgeChunk.text)
            .where(KnowledgeChunk.knowledge_source_id == old_source.id)
            .order_by(KnowledgeChunk.id)
        ).all())
        if existing_texts == chunk_bodies:
            log.debug("index_crm_note: note %d unchanged — skipping re-embed", crm_note_id)
            return 0
        # Changed → drop the stale source + chunks and rebuild below.
        db.execute(_del(KnowledgeChunk).where(KnowledgeChunk.knowledge_source_id == old_source.id))
        db.delete(old_source)
        db.flush()

    venture_name = venture.name if venture else None
    title = note.title or (f"Note — {venture_name}" if venture_name else f"Note {crm_note_id}")
    source = KnowledgeSource(
        source_type="crm_note",
        source_id=crm_note_id,
        crm_venture_id=note.crm_venture_id,
        title=title,
        visibility="admin",
        approved=True,
    )
    db.add(source)
    db.flush()

    # Embed all windows in ONE batched request instead of one per window.
    try:
        vectors = embed_texts(chunk_bodies, api_key)
    except Exception as exc:
        log.warning("index_crm_note: batch embedding failed for note %d: %s", crm_note_id, exc)
        db.rollback()
        return 0

    pending = []
    stored = 0
    for chunk_body, vec in zip(chunk_bodies, vectors):
        themes = infer_basic_themes(chunk_body, sector)
        chunk = KnowledgeChunk(
            knowledge_source_id=source.id,
            crm_venture_id=note.crm_venture_id,
            source_type="crm_note",
            source_id=crm_note_id,
            text=chunk_body,
            sanitized_text=None,
            embedding=_json.dumps(vec),
            sector=sector,
            themes_json=_json.dumps(themes) if themes else None,
            visibility="admin",
            approved=True,
        )
        db.add(chunk)
        db.flush()
        pending.append((chunk.id, vec))
        stored += 1

    db.commit()
    for cid, vec in pending:
        try:
            set_row_vector(db, "knowledge_chunks", cid, vec)
        except Exception as exc:
            log.warning("index_crm_note: pgvector sync skipped: %s", exc)

    return 1 if stored else 0


def index_all_crm_notes(db) -> dict:
    """Re-index every CrmNote. Returns {"indexed": N, "errors": M}."""
    from ..models import CrmNote
    from sqlalchemy import select as _sel

    notes = list(db.scalars(_sel(CrmNote)).all())
    indexed = errors = 0

    for n in notes:
        try:
            indexed += index_crm_note(n.id, db)
        except Exception as exc:
            log.error("index_all_crm_notes: error on note %d: %s", n.id, exc)
            errors += 1

    log.info("index_all_crm_notes: %d indexed, %d errors", indexed, errors)
    return {"indexed": indexed, "errors": errors}


# ---------------------------------------------------------------------------
# CRM File indexing
# ---------------------------------------------------------------------------

def build_crm_file_text(crm_file) -> str:
    """Build indexable text from a CrmFile."""
    parts: list[str] = []
    if crm_file.filename:
        parts.append(f"File: {crm_file.filename}")
    if crm_file.file_type:
        parts.append(f"Type: {crm_file.file_type.upper()}")
    if crm_file.raw_text:
        parts.append(crm_file.raw_text[:8000])
    return "\n".join(parts)


def index_crm_file(crm_file_id: int, db) -> int:
    """Embed and store a single CrmFile as a KnowledgeChunk. Returns 1 or 0."""
    from ..models import CrmFile, KnowledgeSource, KnowledgeChunk
    from .embeddings import embed_text
    from .settings_service import get_openrouter_api_key
    from sqlalchemy import select as _sel, delete as _del
    import json

    f = db.get(CrmFile, crm_file_id)
    if not f or not f.raw_text:
        return 0

    api_key = get_openrouter_api_key(db)
    if not api_key:
        log.warning("index_crm_file: OpenRouter API key not configured")
        return 0

    venture = f.crm_venture if f.crm_venture_id else None
    sector = venture.sector if venture else None
    text_body = build_crm_file_text(f)
    themes = infer_basic_themes(text_body, sector)

    try:
        vec = embed_text(text_body, api_key)
        embedding_json = json.dumps(vec)
    except Exception as exc:
        log.warning("index_crm_file: embedding failed for file %d: %s", crm_file_id, exc)
        return 0

    # Delete old source
    old_src = db.scalar(
        _sel(KnowledgeSource).where(
            KnowledgeSource.source_type == "crm_file",
            KnowledgeSource.source_id == crm_file_id,
        )
    )
    if old_src:
        db.execute(_del(KnowledgeChunk).where(KnowledgeChunk.knowledge_source_id == old_src.id))
        db.delete(old_src)
        db.flush()

    src = KnowledgeSource(
        source_type="crm_file",
        source_id=crm_file_id,
        crm_venture_id=f.crm_venture_id,
        visibility="admin",
        approved=True,
    )
    db.add(src)
    db.flush()

    chunk = KnowledgeChunk(
        knowledge_source_id=src.id,
        source_type="crm_file",
        source_id=crm_file_id,
        crm_venture_id=f.crm_venture_id,
        text=text_body,
        sanitized_text=None,
        embedding=embedding_json,
        sector=sector,
        themes_json=json.dumps(themes) if themes else None,
        visibility="admin",
        approved=True,
    )
    db.add(chunk)
    db.commit()

    try:
        from .vector_store import set_row_vector
        set_row_vector(db, "knowledge_chunks", chunk.id, vec)
    except Exception as exc:
        log.warning("index_crm_file: pgvector sync skipped: %s", exc)

    return 1


def index_all_crm_files(db) -> dict:
    """Re-index every CrmFile that has extracted text."""
    from ..models import CrmFile
    from sqlalchemy import select as _sel

    files = list(db.scalars(_sel(CrmFile).where(CrmFile.raw_text.isnot(None))).all())
    indexed = errors = 0

    for f in files:
        try:
            indexed += index_crm_file(f.id, db)
        except Exception as exc:
            log.error("index_all_crm_files: error on file %d: %s", f.id, exc)
            errors += 1

    log.info("index_all_crm_files: %d indexed, %d errors", indexed, errors)
    return {"indexed": indexed, "errors": errors}


# ---------------------------------------------------------------------------
# Sanitized-text backfill (for note/file chunks indexed before sanitization)
# ---------------------------------------------------------------------------

def backfill_sanitized_text(db) -> dict:
    """
    Generate sanitized_text for existing crm_note / crm_file knowledge chunks
    that don't have it yet. Does NOT re-embed — cheaper than a full re-index.

    Returns {"updated": N, "skipped": M, "errors": E}.
    """
    from ..models import KnowledgeChunk
    from .settings_service import get_openrouter_api_key
    from sqlalchemy import select as _sel
    from sqlalchemy.orm import undefer

    api_key = get_openrouter_api_key(db)
    if not api_key:
        log.warning("backfill_sanitized_text: OpenRouter API key not configured")
        return {"updated": 0, "skipped": 0, "errors": 0, "error": "no_api_key"}

    chunks = list(db.scalars(
        _sel(KnowledgeChunk)
        .options(undefer(KnowledgeChunk.text), undefer(KnowledgeChunk.sanitized_text))
        .where(KnowledgeChunk.source_type.in_(["crm_note", "crm_file"]))
    ).all())

    updated = skipped = errors = 0
    for c in chunks:
        if c.sanitized_text:
            skipped += 1
            continue
        try:
            s = sanitize_text_llm(c.text, api_key)
        except Exception as exc:
            log.warning("backfill_sanitized_text: chunk %d failed: %s", c.id, exc)
            errors += 1
            continue
        if s:
            c.sanitized_text = s
            updated += 1
        else:
            skipped += 1

    db.commit()
    log.info("backfill_sanitized_text: %d updated, %d skipped, %d errors", updated, skipped, errors)
    return {"updated": updated, "skipped": skipped, "errors": errors}
