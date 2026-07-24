"""
Intelligent document indexing for portfolio chat retrieval.

Replaces naive fixed-window chunking with a pipeline designed for
high-quality answers over portfolio company reports:

  1. STRUCTURE-AWARE SPLITTING — the report is split at section headings
     (numbered headings, ALL-CAPS lines, short title-case lines). Paragraphs
     are packed into ~1400-char chunks broken at sentence boundaries with a
     one-sentence overlap. Table-like blocks (financial tables) are kept
     intact in their own chunk so numbers never get sheared apart.

  2. CONTEXTUAL ENRICHMENT (LLM, one call per document) — each chunk gets a
     one-sentence "situating" context generated from the whole document
     (the Anthropic contextual-retrieval pattern). This dramatically improves
     embedding quality for chunks that don't repeat the company name or
     period ("Revenue grew 20%" → "From Acme's Q2 2026 report, financials
     section: revenue grew 20%…").

  3. DOCUMENT SUMMARY CHUNK (LLM, one call per document) — an executive
     summary (key KPIs, highlights, risks, outlook) stored as a
     chunk_type=summary chunk. Broad questions ("how is Acme doing?") match
     this chunk even when no single passage answers them.

  4. RICH METADATA — every chunk stores the reporting period as queryable
     columns (reporting_year/quarter/month) plus a JSON `meta` blob
     (section, chunk index, table flag, category). Retrieval uses the period
     columns for period-aware boosting.

Everything degrades gracefully: if the LLM enrichment or summary call fails,
chunks are still indexed with their structural context header.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# Target chunk size (chars). ~1400 chars ≈ 350 tokens: small enough to be
# precise, large enough to carry a full thought + its context header.
_TARGET = 1400
_HARD_MAX = 2200          # never exceed (tables excepted)
_MIN_CHUNK = 200          # merge tiny trailing chunks into the previous one
_MAX_LLM_DOC_CHARS = 60_000   # cap doc text sent for enrichment/summary
_MAX_CHUNKS_ENRICH = 60       # cap chunks enriched per document


# ---------------------------------------------------------------------------
# 1. Structure-aware splitting
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^(?:"
    r"\d{1,2}[.)]\s+\S"           # "1. Financials" / "2) Team"
    r"|[A-Z][A-Z0-9 &/\-]{3,60}$"  # "FINANCIAL OVERVIEW"
    r"|#{1,4}\s+\S"                # markdown headings
    r")"
)

_LIKELY_HEADING_WORDS = (
    "summary", "highlights", "financial", "revenue", "kpi", "metrics",
    "team", "hiring", "product", "outlook", "risks", "challenges", "asks",
    "runway", "cash", "funding", "pipeline", "customers", "sales", "overview",
)


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if _HEADING_RE.match(line):
        return True
    # Short title-case line without terminal punctuation, containing a
    # section-ish word: "Financial Overview", "Team update"
    if (len(line.split()) <= 6 and line[-1] not in ".:;,!?"
            and any(w in line.lower() for w in _LIKELY_HEADING_WORDS)):
        return True
    return False


def _is_table_block(block: str) -> bool:
    """Heuristic: dense in digits/separators across multiple lines."""
    lines = [l for l in block.splitlines() if l.strip()]
    if len(lines) < 3:
        return False
    hits = 0
    for l in lines:
        digits = sum(ch.isdigit() for ch in l)
        seps = l.count("|") + l.count("\t") + l.count("  ")
        if digits >= 4 or seps >= 3:
            hits += 1
    return hits / len(lines) > 0.6


def split_sections(raw: str) -> list[tuple[str, str]]:
    """Split raw text into (section_title, section_text) pairs."""
    lines = raw.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = "Document"
    current: list[str] = []
    for line in lines:
        if _is_heading(line):
            if current and any(l.strip() for l in current):
                sections.append((current_title, current))
            current_title = line.strip().lstrip("#").strip()
            current = []
        else:
            current.append(line)
    if current and any(l.strip() for l in current):
        sections.append((current_title, current))
    if not sections:
        sections = [("Document", lines)]
    return [(t, "\n".join(ls).strip()) for t, ls in sections if "\n".join(ls).strip()]


_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def _pack_paragraphs(text: str) -> list[tuple[str, bool]]:
    """Pack a section's paragraphs into chunks. Returns [(text, is_table)]."""
    blocks = re.split(r"\n\s*\n", text)
    chunks: list[tuple[str, bool]] = []
    buf = ""

    def _flush():
        nonlocal buf
        if buf.strip():
            chunks.append((buf.strip(), False))
        buf = ""

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if _is_table_block(block):
            _flush()
            chunks.append((block, True))       # tables stay whole
            continue
        if len(buf) + len(block) + 2 <= _TARGET:
            buf = f"{buf}\n\n{block}" if buf else block
            continue
        # block doesn't fit
        if buf:
            _flush()
        if len(block) <= _HARD_MAX:
            buf = block
            continue
        # very long paragraph: split at sentence boundaries with 1-sentence overlap
        sentences = _SENT_RE.split(block)
        cur, prev_tail = "", ""
        for s in sentences:
            if len(cur) + len(s) + 1 > _TARGET and cur:
                chunks.append(((prev_tail + " " + cur).strip(), False))
                prev_tail = cur.rsplit(". ", 1)[-1][-150:]
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            buf = (prev_tail + " " + cur).strip()
    _flush()

    # merge a tiny trailing chunk into its predecessor
    if len(chunks) >= 2 and len(chunks[-1][0]) < _MIN_CHUNK and not chunks[-1][1]:
        last_text, _ = chunks.pop()
        prev_text, prev_tbl = chunks[-1]
        if not prev_tbl:
            chunks[-1] = (prev_text + "\n\n" + last_text, False)
        else:
            chunks.append((last_text, False))
    return chunks


def build_structured_chunks(raw: str) -> list[dict]:
    """Full structural pass: [{'section', 'text', 'is_table', 'chunk_index'}]."""
    out: list[dict] = []
    i = 0
    for title, body in split_sections(raw):
        for text, is_table in _pack_paragraphs(body):
            out.append({"section": title, "text": text, "is_table": is_table, "chunk_index": i})
            i += 1
    return out


# ---------------------------------------------------------------------------
# 2 + 3. LLM enrichment (contextual sentences + document summary)
# ---------------------------------------------------------------------------

def _llm(messages: list[dict], db) -> str | None:
    """Best-effort LLM call reusing the extraction plumbing. None on failure."""
    try:
        from ..config import settings as _settings
        from .portfolio_extraction import _call_openrouter
        from .settings_service import get_openrouter_api_key
        api_key = get_openrouter_api_key(db)
        if not api_key:
            return None
        return _call_openrouter(messages, api_key=api_key, model=_settings.openrouter_chat_model)
    except Exception as exc:
        log.warning("document_indexer: LLM call failed (%s)", exc)
        return None


def enrich_chunks_llm(doc_header: str, raw: str, chunks: list[dict], db) -> None:
    """One LLM call: a situating sentence per chunk, stored as chunk['context']."""
    subset = chunks[:_MAX_CHUNKS_ENRICH]
    previews = [
        {"i": c["chunk_index"], "section": c["section"], "preview": c["text"][:400]}
        for c in subset
    ]
    prompt = (
        "You are indexing a portfolio company report for a retrieval system.\n"
        f"Document: {doc_header}\n\n"
        f"Document text (may be truncated):\n{raw[:_MAX_LLM_DOC_CHARS]}\n\n"
        "For EACH chunk below, write ONE short sentence (max 25 words) that situates "
        "it within the document: what it is about, which company/period it concerns, "
        "and any key figures' meaning. This sentence will be prepended to the chunk "
        "for embedding.\n\n"
        f"Chunks:\n{json.dumps(previews, ensure_ascii=False)}\n\n"
        'Respond with ONLY a JSON object mapping chunk index to sentence, e.g. '
        '{"0": "…", "1": "…"}.'
    )
    raw_resp = _llm([{"role": "user", "content": prompt}], db)
    if not raw_resp:
        return
    try:
        from .portfolio_extraction import parse_llm_json
        mapping = parse_llm_json(raw_resp)
        for c in subset:
            ctx = mapping.get(str(c["chunk_index"]))
            if isinstance(ctx, str) and ctx.strip():
                c["context"] = ctx.strip()[:300]
    except Exception as exc:
        log.warning("document_indexer: could not parse enrichment response (%s)", exc)


def summarize_document_llm(doc_header: str, raw: str, db) -> str | None:
    """Executive summary of the report, used as a summary chunk."""
    prompt = (
        "Summarize this portfolio company report for an investor-facing knowledge "
        "base. Structure the summary as short lines covering: key financial KPIs "
        "(revenue, growth, burn, runway, cash — with numbers), business highlights, "
        "team changes, product updates, risks/challenges, outlook and asks. "
        "Be specific and quantitative; max 300 words.\n\n"
        f"Document: {doc_header}\n\n{raw[:_MAX_LLM_DOC_CHARS]}"
    )
    resp = _llm([{"role": "user", "content": prompt}], db)
    return resp.strip() if resp else None


# ---------------------------------------------------------------------------
# 4. Orchestrator
# ---------------------------------------------------------------------------

def index_document(document_id: int, db) -> int:
    """
    Index a document's full text into intelligent, metadata-rich chunks.
    Replaces existing text/summary chunks for the document (idempotent).
    Returns the number of chunks stored.
    """
    from sqlalchemy import select

    from ..models import Chunk, ChunkType, Company, Document
    from .embeddings import embed_texts
    from .settings_service import get_openrouter_api_key

    doc = db.get(Document, document_id)
    if not doc:
        raise ValueError(f"Document {document_id} not found.")
    raw = (doc.raw_text or "").strip()
    if len(raw) < 20:
        raise ValueError(f"Document {document_id} has no usable raw text.")

    api_key = get_openrouter_api_key(db)
    if not api_key:
        raise ValueError("OpenRouter API key is not configured.")

    company = db.get(Company, doc.company_id)
    category = doc.document_category.value.replace("_", " ") if doc.document_category else None
    header_bits = [company.name if company else None, category,
                   doc.reporting_period, doc.title]
    doc_header = " — ".join(b for b in header_bits if b)

    # 1. structural chunks
    chunks = build_structured_chunks(raw)
    if not chunks:
        raise ValueError(f"Document {document_id}: chunking produced no text.")

    # 2. contextual enrichment (best-effort)
    enrich_chunks_llm(doc_header, raw, chunks, db)

    # 3. document summary (best-effort)
    summary = summarize_document_llm(doc_header, raw, db)

    # Build embedding texts: header + situating context + body
    texts: list[str] = []
    for c in chunks:
        ctx = c.get("context")
        head = f"[{doc_header} | {c['section']}]"
        texts.append(f"{head}\n{ctx}\n\n{c['text']}" if ctx else f"{head}\n{c['text']}")
    if summary:
        texts.append(f"[{doc_header} | Executive summary]\n{summary}")

    vectors = embed_texts(texts, api_key)

    # Replace previous raw-text + summary chunks
    existing = db.scalars(
        select(Chunk).where(
            Chunk.document_id == document_id,
            Chunk.chunk_type.in_([ChunkType.text, ChunkType.summary]),
        )
    ).all()
    for old in existing:
        db.delete(old)
    db.flush()

    created: list[tuple[Chunk, list[float]]] = []

    def _mk(text: str, vector: list[float], ctype, meta: dict) -> None:
        chunk = Chunk(
            document_id=document_id,
            extraction_id=None,
            company_id=doc.company_id,
            chunk_type=ctype,
            text=text,
            embedding=json.dumps(vector),
            approved=True,
            reporting_year=doc.reporting_year,
            reporting_quarter=doc.reporting_quarter,
            reporting_month=doc.reporting_month,
            meta=json.dumps(meta, ensure_ascii=False),
        )
        db.add(chunk)
        created.append((chunk, vector))

    from ..models import ChunkType as _CT
    for c, (text, vector) in zip(chunks, zip(texts, vectors)):
        _mk(text, vector, _CT.text, {
            "section": c["section"], "chunk_index": c["chunk_index"],
            "is_table": c["is_table"], "category": category,
            "period": doc.reporting_period, "context": c.get("context"),
        })
    if summary:
        _mk(texts[-1], vectors[-1], _CT.summary, {
            "section": "Executive summary", "category": category,
            "period": doc.reporting_period,
        })

    db.commit()

    try:
        from .vector_store import set_row_vector
        for chunk, vector in created:
            set_row_vector(db, "chunks", chunk.id, vector)
    except Exception as exc:
        log.warning("index_document: pgvector sync skipped: %s", exc)

    log.info(
        "index_document: stored %d chunk(s) for document %d (%d sections, summary=%s)",
        len(created), document_id, len({c['section'] for c in chunks}), bool(summary),
    )
    return len(created)
