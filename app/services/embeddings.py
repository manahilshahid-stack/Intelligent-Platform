"""
Embedding service for approved portfolio extractions.

Flow
----
1. extraction_to_text(corrected_json)  -> readable prose string
2. chunk_text(text, ...)               -> list[str]
3. embed_text(text, api_key)           -> list[float]   (one OpenRouter call)
4. embed_approved_extraction(extraction_id, db)
       Orchestrates the above, writes Chunk rows, stores embeddings as JSON.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

OPENROUTER_EMBEDDING_URL = "https://openrouter.ai/api/v1/embeddings"
REQUEST_TIMEOUT = 60


# ---------------------------------------------------------------------------
# 1. extraction_to_text
# ---------------------------------------------------------------------------

def extraction_to_text(corrected_json: str, doc_meta: dict | None = None) -> str:
    """
    Convert a corrected_json string into a human-readable text block
    suitable for embedding.  Returns an empty string if the JSON is invalid.

    Parameters
    ----------
    corrected_json : the approved/corrected JSON string from an Extraction row
    doc_meta       : optional dict with document-level metadata:
                       category (str), reporting_period (str | None),
                       reporting_year/month/quarter (int | None),
                       excluded_standard_kpis (set[str] | None)
    """
    try:
        d = json.loads(corrected_json)
    except Exception:
        return ""

    lines: list[str] = []

    def _add(label: str, value) -> None:
        if value is not None and str(value).strip():
            lines.append(f"{label}: {value}")

    def _kpi(label: str, block: dict | None) -> None:
        """Emit KPI line plus source snippet if present."""
        if not isinstance(block, dict):
            return
        val = block.get("value")
        if val is None:
            return
        cur = block.get("currency", "")
        period = block.get("period", "")
        parts = [str(val)]
        if cur:
            parts.append(cur)
        if period:
            parts.append(f"({period})")
        lines.append(f"{label}: {' '.join(parts)}")
        src = block.get("source_text")
        if src and str(src).strip():
            lines.append(f"  [{src.strip()}]")

    def _list(label: str, items) -> None:
        if not isinstance(items, list) or not items:
            return
        lines.append(f"{label}:")
        for item in items:
            if item and str(item).strip():
                lines.append(f"  - {item}")

    _add("Company", d.get("company_name"))
    _add("Document", d.get("document_title"))

    # Document-level metadata
    if doc_meta:
        cat = doc_meta.get("category")
        if cat:
            _add("Category", cat.replace("_", " ").title())
        period = doc_meta.get("reporting_period")
        if period:
            _add("Reporting period", period)
        elif doc_meta.get("reporting_year"):
            y = doc_meta["reporting_year"]
            m = doc_meta.get("reporting_month")
            q = doc_meta.get("reporting_quarter")
            if m:
                import calendar
                _add("Reporting period", f"{calendar.month_name[m]} {y}")
            elif q:
                _add("Reporting period", f"Q{q} {y}")
            else:
                _add("Reporting year", str(y))

    _add("Period", d.get("period"))

    biz = d.get("business_description")
    if biz and str(biz).strip():
        lines.append(f"Business: {biz.strip()}")

    lines.append("")

    excl: set[str] = set()
    if doc_meta:
        excl = doc_meta.get("excluded_standard_kpis") or set()

    if "cash_position" not in excl:
        _kpi("Cash position", d.get("cash_position"))
    if "monthly_burn" not in excl:
        _kpi("Monthly burn", d.get("monthly_burn"))

    if "runway_months" not in excl:
        rm = d.get("runway_months")
        if isinstance(rm, dict) and rm.get("value") is not None:
            lines.append(f"Runway: {rm['value']} months")
            src = rm.get("source_text")
            if src:
                lines.append(f"  [{src.strip()}]")

    if "revenue" not in excl:
        _kpi("Revenue", d.get("revenue"))

    # MRR — always include regardless of exclusion list (separate from revenue)
    _kpi("MRR", d.get("mrr"))

    if "arr" not in excl:
        _kpi("ARR", d.get("arr"))

    # Gross margin
    gm = d.get("gross_margin")
    if isinstance(gm, dict) and gm.get("value") is not None:
        lines.append(f"Gross margin: {gm['value']}%")
        src = gm.get("source_text")
        if src:
            lines.append(f"  [{src.strip()}]")

    # Growth metrics
    gro = d.get("growth_metrics")
    if isinstance(gro, dict):
        if gro.get("mom_growth_pct") is not None:
            lines.append(f"MoM growth: {gro['mom_growth_pct']}%")
        if gro.get("yoy_growth_pct") is not None:
            lines.append(f"YoY growth: {gro['yoy_growth_pct']}%")
        if gro.get("description"):
            lines.append(f"Growth: {gro['description']}")
        src = gro.get("source_text")
        if src and (gro.get("mom_growth_pct") is not None
                    or gro.get("yoy_growth_pct") is not None
                    or gro.get("description")):
            lines.append(f"  [{src.strip()}]")

    if "headcount" not in excl:
        hc = d.get("headcount")
        if isinstance(hc, dict) and hc.get("value") is not None:
            lines.append(f"Headcount: {hc['value']}")
            src = hc.get("source_text")
            if src:
                lines.append(f"  [{src.strip()}]")

    if "customers" not in excl:
        cu = d.get("customers")
        if isinstance(cu, dict) and cu.get("value") is not None:
            lines.append(f"Customers: {cu['value']}")
            src = cu.get("source_text")
            if src:
                lines.append(f"  [{src.strip()}]")

    lines.append("")
    _list("Key wins", d.get("key_wins"))
    _list("Key challenges", d.get("key_challenges"))
    _list("Risks", d.get("risks"))
    _list("Asks", d.get("asks"))
    _list("Next milestones", d.get("next_milestones"))

    # Custom KPIs
    custom_kpis = d.get("custom_kpis")
    if isinstance(custom_kpis, dict) and custom_kpis:
        lines.append("")
        lines.append("Custom KPIs:")
        for fk, entry in custom_kpis.items():
            if not isinstance(entry, dict):
                continue
            val = entry.get("value")
            if val is None:
                continue
            label = entry.get("label") or fk
            lines.append(f"  {label}: {val}")
            src = entry.get("source_text")
            if src:
                lines.append(f"    [{src.strip()}]")

    summary = d.get("summary")
    if summary and str(summary).strip():
        lines.append("")
        lines.append(f"Summary: {summary}")

    confidence = d.get("confidence")
    if confidence:
        lines.append(f"Confidence: {confidence}")

    missing = d.get("missing_fields")
    if isinstance(missing, list) and missing:
        lines.append(f"Missing fields: {', '.join(missing)}")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# 2. chunk_text
# ---------------------------------------------------------------------------

def chunk_text(
    text: str,
    max_chars: int = 3000,
    overlap_chars: int = 300,
) -> list[str]:
    """
    Split text into overlapping windows.

    Windows are broken at word boundaries where possible.
    Returns a list of non-empty strings.
    """
    if not text:
        return []

    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunk = text[start:]
        else:
            # Try to break at last newline within the window, else last space
            break_at = text.rfind("\n", start, end)
            if break_at <= start:
                break_at = text.rfind(" ", start, end)
            if break_at <= start:
                break_at = end  # hard cut
            chunk = text[start : break_at].rstrip()
            end = break_at

        if chunk:
            chunks.append(chunk)

        start = end - overlap_chars
        if start >= len(text):
            break

    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# 3. embed_text
# ---------------------------------------------------------------------------

def embed_text(text: str, api_key: str) -> list[float]:
    """
    Call the OpenRouter embeddings endpoint and return the embedding vector.
    Raises RuntimeError on failure.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://portfolio-intelligence.app",
        "X-Title": "Portfolio Intelligence Platform",
    }
    from ..config import settings as _settings
    payload = {
        "model": _settings.openrouter_embedding_model,
        "input": text,
    }

    try:
        resp = httpx.post(
            OPENROUTER_EMBEDDING_URL,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Embedding request timed out after {REQUEST_TIMEOUT}s.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Embedding request failed: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"OpenRouter embeddings returned HTTP {resp.status_code}: {resp.text[:400]}"
        )

    try:
        data = resp.json()
        vector = data["data"][0]["embedding"]
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"Unexpected embedding response shape: {exc}") from exc

    if not isinstance(vector, list) or not vector:
        raise RuntimeError("Embedding API returned an empty vector.")

    return vector


# ---------------------------------------------------------------------------
# 3b. embed_texts — batched embeddings (many inputs per request)
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str], api_key: str, batch_size: int = 64) -> list[list[float]]:
    """
    Embed many texts using as few OpenRouter requests as possible.

    The OpenRouter/OpenAI embeddings endpoint accepts a LIST of inputs and
    returns one vector per input, so this collapses N single-text calls into
    ceil(N / batch_size) calls — the main indexing speed-up.

    Order is preserved (results are re-sorted by the API's `index` field).
    Raises RuntimeError on failure. Returns [] for empty input.
    """
    if not texts:
        return []

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://portfolio-intelligence.app",
        "X-Title": "Portfolio Intelligence Platform",
    }
    from ..config import settings as _settings

    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        payload = {
            "model": _settings.openrouter_embedding_model,
            "input": batch,
        }
        try:
            resp = httpx.post(
                OPENROUTER_EMBEDDING_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Embedding batch timed out after {REQUEST_TIMEOUT}s."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Embedding batch request failed: {exc}") from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"OpenRouter embeddings returned HTTP {resp.status_code}: {resp.text[:400]}"
            )

        try:
            data = resp.json()
            items = data["data"]
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"Unexpected embedding response shape: {exc}") from exc

        # Re-sort by `index` so vectors line up with the input order.
        items_sorted = sorted(items, key=lambda d: d.get("index", 0))
        if len(items_sorted) != len(batch):
            raise RuntimeError(
                f"Embedding batch returned {len(items_sorted)} vectors for {len(batch)} inputs."
            )
        for it in items_sorted:
            vec = it.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise RuntimeError("Embedding API returned an empty vector in a batch.")
            out.append(vec)

    return out


# ---------------------------------------------------------------------------
# 4. embed_approved_extraction
# ---------------------------------------------------------------------------

def embed_approved_extraction(extraction_id: int, db: "Session") -> int:
    """
    Build chunks from the extraction's corrected_json, embed each chunk,
    and persist to the chunks table.

    Returns the number of chunks stored.
    Raises ValueError if the extraction or API key is missing.
    Raises RuntimeError on embedding API failure.
    """
    from sqlalchemy import select

    from ..models import Chunk, ChunkType, Extraction
    from .settings_service import get_openrouter_api_key

    extraction = db.get(Extraction, extraction_id)
    if not extraction:
        raise ValueError(f"Extraction {extraction_id} not found.")

    json_src = extraction.corrected_json or extraction.extracted_json
    if not json_src:
        raise ValueError(f"Extraction {extraction_id} has no JSON to embed.")

    api_key = get_openrouter_api_key(db)
    if not api_key:
        raise ValueError("OpenRouter API key is not configured.")

    # Delete existing approved_extraction chunks for this extraction
    existing = db.scalars(
        select(Chunk).where(
            Chunk.extraction_id == extraction_id,
            Chunk.chunk_type == ChunkType.approved_extraction,
        )
    ).all()
    for chunk in existing:
        db.delete(chunk)
    db.flush()

    # Load document metadata for richer embedding text
    from ..models import CompanyReportingSettings, Document as _Document
    doc = db.get(_Document, extraction.document_id)
    doc_meta: dict | None = None
    if doc:
        # Load excluded standard KPIs for this company
        settings = db.scalar(
            select(CompanyReportingSettings).where(
                CompanyReportingSettings.company_id == doc.company_id
            )
        )
        excluded: set[str] = set()
        if settings and settings.excluded_standard_kpis:
            try:
                excluded = set(json.loads(settings.excluded_standard_kpis))
            except Exception:
                pass

        doc_meta = {
            "category": doc.document_category.value if doc.document_category else None,
            "reporting_period": doc.reporting_period,
            "reporting_year": doc.reporting_year,
            "reporting_month": doc.reporting_month,
            "reporting_quarter": doc.reporting_quarter,
            "excluded_standard_kpis": excluded,
        }

    # Build readable text and split into chunks
    readable = extraction_to_text(json_src, doc_meta=doc_meta)
    if not readable:
        raise ValueError(f"Extraction {extraction_id}: no readable text could be derived.")

    texts = chunk_text(readable)
    log.info(
        "Embedding extraction %d: %d chunk(s) from %d chars",
        extraction_id, len(texts), len(readable),
    )

    # Embed all chunks in one batched request (was one request per chunk).
    try:
        vectors = embed_texts(texts, api_key)
    except RuntimeError as exc:
        log.error("Failed to embed extraction %d: %s", extraction_id, exc)
        raise

    stored = 0
    created: list[tuple[object, list[float]]] = []
    for text, vector in zip(texts, vectors):
        chunk = Chunk(
            document_id=extraction.document_id,
            extraction_id=extraction_id,
            company_id=extraction.company_id,
            chunk_type=ChunkType.approved_extraction,
            text=text,
            embedding=json.dumps(vector),
            approved=True,
        )
        db.add(chunk)
        created.append((chunk, vector))
        stored += 1

    db.commit()

    # Populate the pgvector column so new chunks are immediately searchable
    # (no-op on SQLite / when pgvector is unavailable).
    try:
        from .vector_store import set_row_vector
        for chunk, vector in created:
            set_row_vector(db, "chunks", chunk.id, vector)
    except Exception as exc:
        log.warning("embed_approved_extraction: pgvector sync skipped: %s", exc)

    log.info("Stored %d embedded chunk(s) for extraction %d", stored, extraction_id)
    return stored
