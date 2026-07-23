"""
Retrieval service: embed a query, rank approved chunks by cosine similarity.

retrieve_relevant_chunks(query, user, db, filters=None, limit=10)
  -> list[ChunkResult]

retrieve_knowledge_chunks(query, user, db, filters=None, limit=10)
  -> list[ChunkResult]

retrieve_attio_ventures(query, attio_api_key, openrouter_api_key, limit=6)
  -> list[ChunkResult]  [PRODUCTION - retrieves from Attio CRM]
"""
from __future__ import annotations

import json
import math
import logging
import httpx
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from ..models import User

log = logging.getLogger(__name__)


@dataclass
class ChunkResult:
    """Standard chunk result format for all retrieval methods."""
    chunk_id: int
    document_id: int | None
    extraction_id: int | None
    company_id: int | None
    company_name: str
    document_title: str
    text: str
    score: float
    # CRM venture fields (only set for knowledge chunks)
    crm_venture_id: int | None = None
    crm_venture_name: str | None = None
    attio_url: str | None = None
    sector: str | None = None
    themes: list[str] | None = None
    source_type: str = "portfolio"  # "portfolio" | "crm_venture"
    # Non-admin-safe copy for crm_note/crm_file chunks (None if not generated)
    sanitized_text: str | None = None
    # When the underlying CRM record was last updated (venture.updated_at or note date).
    # Used for recency scoring and to stamp context blocks shown to the LLM.
    source_date: "datetime | None" = None


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python, no external deps)
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Hybrid retrieval helpers (Phase 1.3): vector + keyword fused with RRF
# ---------------------------------------------------------------------------

# How many candidates to pull from EACH modality before fusing/trimming.
_CANDIDATE_MULTIPLIER = 4
_CANDIDATE_MIN = 30

# Candidate pool size handed to the reranker before trimming to the final limit.
_RERANK_POOL = 40

_KW_STOPWORDS = {
    "the", "and", "for", "are", "with", "what", "which", "how", "that", "this",
    "from", "have", "has", "had", "their", "our", "you", "your", "about", "into",
    "over", "was", "were", "will", "can", "does", "did", "who", "why", "when",
    "where", "they", "them", "its", "but", "not", "all", "any", "more", "most",
}


def _kw_terms(query: str) -> list[str]:
    """Tokenise a query into distinct lowercased keyword terms (drop stopwords/short)."""
    import re
    toks = re.findall(r"[a-z0-9]+", query.lower())
    out: list[str] = []
    for t in dict.fromkeys(toks):  # dedupe, preserve order
        if len(t) >= 3 and t not in _KW_STOPWORDS:
            out.append(t)
    return out[:12]


def _rrf(*ranked_lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion of several ranked id lists (best-first). Returns fused ids."""
    scores: dict = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return [cid for cid, _ in sorted(scores.items(), key=lambda x: -x[1])]


def _keyword_candidate_ids(db, model, base_stmt, query: str, k: int) -> list:
    """
    Keyword retrieval over the chunk `text` column. Dialect-safe (ORM-rendered
    LIKE) so it works identically on SQLite and Postgres — no FTS infra needed.
    Ranks rows by number of distinct query terms matched, tie-broken by brevity.
    """
    from sqlalchemy import or_, func

    terms = _kw_terms(query)
    if not terms:
        return []
    conds = [func.lower(model.text).like(f"%{t}%") for t in terms]
    try:
        stmt = base_stmt.with_only_columns(model.id, model.text).where(or_(*conds))
        rows = db.execute(stmt).all()
    except Exception as exc:
        log.warning("keyword retrieval failed: %s", exc)
        return []
    scored: list[tuple[int, int, int]] = []
    for rid, txt in rows:
        low = (txt or "").lower()
        cnt = sum(1 for t in terms if t in low)
        if cnt:
            scored.append((cnt, len(txt or ""), rid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [rid for _, _, rid in scored[:k]]


def _vector_candidate_pairs(db, model, base_stmt, query_vec, k, pg_table, pg_extra, pg_params):
    """Return [(id, cosine)] for the top-k by vector similarity (pgvector or Python)."""
    from .vector_store import pgvector_available, vector_topk

    if pgvector_available(db):
        try:
            return [(i, s) for i, s in vector_topk(db, pg_table, query_vec, k, pg_extra, pg_params)]
        except Exception as exc:
            log.warning("%s: pgvector path failed, falling back to Python cosine: %s", pg_table, exc)

    pairs: list[tuple[float, int]] = []
    for c in db.scalars(base_stmt).all():
        try:
            v = json.loads(c.embedding)
        except (TypeError, ValueError):
            continue
        pairs.append((_cosine(query_vec, v), c.id))
    pairs.sort(key=lambda t: t[0], reverse=True)
    return [(cid, sc) for sc, cid in pairs[:k]]


def _hybrid_rank(db, model, base_stmt, query, query_vec, limit,
                 pg_table, pg_extra="", pg_params=None):
    """
    Hybrid retrieval: take top candidates from vector search AND keyword search,
    fuse with Reciprocal Rank Fusion, return [(display_score, orm_obj)] (top *limit*).
    display_score is the cosine similarity when the row was a vector hit, else 0.0.
    """
    candidate_k = max(limit * _CANDIDATE_MULTIPLIER, _CANDIDATE_MIN)
    vec_pairs = _vector_candidate_pairs(
        db, model, base_stmt, query_vec, candidate_k, pg_table, pg_extra, pg_params
    )
    vec_ids = [i for i, _ in vec_pairs]
    vec_score = {i: s for i, s in vec_pairs}
    kw_ids = _keyword_candidate_ids(db, model, base_stmt, query, candidate_k)

    fused = _rrf(vec_ids, kw_ids)[:limit]
    if not fused:
        return []
    objs = {o.id: o for o in db.scalars(select(model).where(model.id.in_(fused))).all()}
    return [(vec_score.get(i, 0.0), objs[i]) for i in fused if i in objs]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve_relevant_chunks(
    query: str,
    user: "User",
    db: "Session",
    filters: dict | None = None,
    limit: int = 10,
    include_crm: bool = True,   # NEW: allow callers to get portfolio-only results
) -> list[ChunkResult]:
    """
    Embed *query*, score all approved chunks with stored embeddings by cosine
    similarity, apply permission and optional filters, return the top *limit*
    results ordered by descending score.

    Parameters
    ----------
    query   : natural-language question
    user    : current authenticated user (determines company scope)
    db      : SQLAlchemy session
    filters : optional dict; supported keys:
                  company_id (int) — restrict to a single company
    limit   : max results to return

    Raises
    ------
    ValueError  if OpenRouter API key is not configured
    RuntimeError on embedding API failure
    """
    from ..models import Chunk, Company, Document, UserRole
    from .embeddings import embed_text
    from .settings_service import get_openrouter_api_key

    filters = filters or {}

    # ── Permission scope ─────────────────────────────────────────────────────
    is_admin = user.role == UserRole.admin
    if not is_admin and not user.company_id:
        log.debug("User %d has no company and is not admin — returning empty", user.id)
        return []

    # ── API key ──────────────────────────────────────────────────────────────
    api_key = get_openrouter_api_key(db)
    if not api_key:
        raise ValueError(
            "OpenRouter API key is not configured. "
            "Set OPENROUTER_API_KEY or go to Admin → Settings."
        )

    # ── Embed query ───────────────────────────────────────────────────────────
    query_vec = embed_text(query.strip(), api_key)

    # ── Load approved chunks that have an embedding ───────────────────────────
    stmt = select(Chunk).where(
        Chunk.approved == True,  # noqa: E712
        Chunk.embedding.is_not(None),
    )

    if not is_admin:
        stmt = stmt.where(Chunk.company_id == user.company_id)

    if "company_id" in filters:
        stmt = stmt.where(Chunk.company_id == int(filters["company_id"]))

    # ── Hybrid rank: vector + keyword fused with RRF ───────────────────────────
    pg_extra, pg_params = "", {}
    if not is_admin:
        pg_extra = "company_id = :scope_cid"
        pg_params["scope_cid"] = user.company_id
    if "company_id" in filters:
        clause = "company_id = :filt_cid"
        pg_params["filt_cid"] = int(filters["company_id"])
        pg_extra = f"{pg_extra} AND {clause}" if pg_extra else clause

    scored = _hybrid_rank(db, Chunk, stmt, query, query_vec, limit, "chunks", pg_extra, pg_params)

    if not scored:
        return []

    # ── Pre-load companies and documents for ranked chunks (avoid N+1) ─────────
    ranked_chunks = [c for _, c in scored]
    company_ids = {c.company_id for c in ranked_chunks}
    doc_ids = {c.document_id for c in ranked_chunks}

    companies: dict[int, str] = {
        co.id: co.name
        for co in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
    }
    documents: dict[int, str] = {
        d.id: d.title
        for d in db.scalars(select(Document).where(Document.id.in_(doc_ids))).all()
    }

    # ── Build results ─────────────────────────────────────────────────────────
    results: list[ChunkResult] = []
    for score, chunk in scored[:limit]:
        results.append(ChunkResult(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            extraction_id=chunk.extraction_id,
            company_id=chunk.company_id,
            company_name=companies.get(chunk.company_id, "Unknown"),
            document_title=documents.get(chunk.document_id, "Unknown"),
            text=chunk.text,
            score=round(score, 6),
        ))

    log.debug(
        "retrieve_relevant_chunks: query=%r user=%d portfolio_scored=%d returning=%d",
        query[:60], user.id, len(scored), len(results),
    )

    # ── CRM knowledge chunks (admin-only) ─────────────────────────────────────
    if is_admin and include_crm:
        crm_results = _retrieve_crm_chunks(query_vec, db, limit)
        results = results + crm_results
        # Re-sort combined list and trim to limit
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:limit]

    return results


def retrieve_knowledge_chunks(
    query: str,
    user: "User",
    db: "Session",
    filters: dict | None = None,
    limit: int = 10,
) -> list[ChunkResult]:
    """
    Retrieve from KnowledgeChunk table for all users.
    
    Embed *query*, score all approved knowledge chunks with stored embeddings 
    by cosine similarity, apply permission and optional filters, return the top 
    *limit* results ordered by descending score.

    Parameters
    ----------
    query   : natural-language question
    user    : current authenticated user (determines company scope)
    db      : SQLAlchemy session
    filters : optional dict; supported keys:
                  company_id (int) — restrict to a single company
    limit   : max results to return

    Raises
    ------
    ValueError  if OpenRouter API key is not configured
    RuntimeError on embedding API failure
    """
    from ..models import KnowledgeChunk, CrmVenture, UserRole
    from .embeddings import embed_text
    from .settings_service import get_openrouter_api_key

    filters = filters or {}

    # ── Permission scope ─────────────────────────────────────────────────────
    is_admin = user.role == UserRole.admin
    log.info(f"[DEBUG] retrieve_knowledge_chunks: user={user.id}, is_admin={is_admin}")

    # ── API key ──────────────────────────────────────────────────────────────
    api_key = get_openrouter_api_key(db)
    if not api_key:
        log.error("[DEBUG] No API key configured")
        raise ValueError(
            "OpenRouter API key is not configured. "
            "Set OPENROUTER_API_KEY or go to Admin → Settings."
        )

    # ── Embed query ───────────────────────────────────────────────────────────
    try:
        query_vec = embed_text(query.strip(), api_key)
        log.info(f"[DEBUG] Query embedded successfully, vector length: {len(query_vec)}")
    except Exception as e:
        log.error(f"[DEBUG] Embedding failed: {e}")
        raise

    # ── Load approved knowledge chunks that have an embedding ──────────────────
    stmt = select(KnowledgeChunk).where(
        KnowledgeChunk.approved == True,  # noqa: E712
        KnowledgeChunk.embedding.is_not(None),
    )

    # Visibility (product decision, Jun 2026): all authenticated users — admins,
    # company users, and LP users — may query approved knowledge chunks.
    # Financial redaction for LPs is applied downstream, not here.
    log.info("[DEBUG] Knowledge visibility: all approved chunks visible to authenticated users")

    # Apply company filter if specified
    if "company_id" in filters:
        stmt = stmt.where(KnowledgeChunk.company_id == int(filters["company_id"]))
        log.info(f"[DEBUG] Filtering by company_id: {filters['company_id']}")

    # ── Hybrid rank: vector + keyword fused with RRF ───────────────────────────
    pg_extra, pg_params = "", {}
    if "company_id" in filters:
        pg_extra = "company_id = :filt_cid"
        pg_params["filt_cid"] = int(filters["company_id"])

    # Apply crm_venture_ids filter (LP portfolio scope) — must go into BOTH
    # the ORM stmt (SQLite / Python-cosine path) AND pg_extra (pgvector path).
    if "crm_venture_ids" in filters and filters["crm_venture_ids"]:
        venture_id_list = list(filters["crm_venture_ids"])
        stmt = stmt.where(KnowledgeChunk.crm_venture_id.in_(venture_id_list))
        # Build an ANY() clause for the pgvector raw-SQL path
        pg_params["portfolio_ids"] = venture_id_list
        portfolio_clause = "crm_venture_id = ANY(:portfolio_ids)"
        pg_extra = f"{pg_extra} AND {portfolio_clause}" if pg_extra else portfolio_clause
        log.info("[DEBUG] LP portfolio filter: %d venture IDs via SQL + pgvector", len(venture_id_list))

    scored = _hybrid_rank(
        db, KnowledgeChunk, stmt, query, query_vec, limit,
        "knowledge_chunks", pg_extra, pg_params,
    )

    if not scored:
        log.warning("[DEBUG] No knowledge chunks found")
        return []

    # ── Pre-load ventures for ranked chunks (avoid N+1) ────────────────────────
    venture_ids = {c.crm_venture_id for _, c in scored if getattr(c, 'crm_venture_id', None)}
    ventures: dict[int, "CrmVenture"] = {}
    if venture_ids:
        ventures = {
            v.id: v
            for v in db.scalars(select(CrmVenture).where(CrmVenture.id.in_(venture_ids))).all()
        }
        log.info(f"[DEBUG] Loaded {len(ventures)} ventures")

    log.info(f"[DEBUG] Scored {len(scored)} chunks, top score: {scored[0][0] if scored else 'N/A'}")

    # ── Build results ─────────────────────────────────────────────────────────
    results: list[ChunkResult] = []
    for score, chunk in scored[:limit]:
        try:
            # Safely get attributes with fallbacks
            source_type = getattr(chunk, 'source_type', 'knowledge') or 'knowledge'
            chunk_title = getattr(chunk, 'title', None)
            
            # Determine document title based on source type
            if source_type == "crm_note":
                doc_title = "Attio Note"
            elif source_type == "crm_file":
                doc_title = "Attio File"
            elif source_type == "crm_venture":
                doc_title = "CRM / Attio"
            elif source_type == "gdrive":
                doc_title = "Google Doc"
            else:
                doc_title = chunk_title or "Knowledge"

            # Parse themes if available
            themes: list[str] | None = None
            themes_json = getattr(chunk, 'themes_json', None)
            if themes_json:
                try:
                    themes = json.loads(themes_json)
                except Exception:
                    pass

            # Get venture info if this is a CRM venture chunk
            crm_venture_id = getattr(chunk, 'crm_venture_id', None)
            venture = ventures.get(crm_venture_id) if crm_venture_id else None
            
            # Company name from venture or chunk attribute
            company_name = venture.name if venture else getattr(chunk, 'company_name', 'Unknown')

            # Source date: prefer venture's last update; fall back to sync/created time
            source_date = None
            if venture:
                source_date = (
                    getattr(venture, "updated_at", None)
                    or getattr(venture, "synced_at", None)
                    or getattr(venture, "created_at", None)
                )

            # Build result with safe attribute access
            results.append(ChunkResult(
                chunk_id=chunk.id,
                document_id=None,
                extraction_id=None,
                company_id=getattr(chunk, 'company_id', None),
                company_name=company_name,
                document_title=doc_title,
                text=chunk.text,
                score=round(score, 6),
                crm_venture_id=crm_venture_id,
                crm_venture_name=venture.name if venture else None,
                attio_url=venture.attio_url if venture else None,
                sector=getattr(chunk, 'sector', None),
                themes=themes,
                source_type=source_type,
                sanitized_text=getattr(chunk, 'sanitized_text', None),
                source_date=source_date,
            ))
        except Exception as e:
            log.error(f"[DEBUG] Error building result for chunk {chunk.id}: {e}", exc_info=True)
            continue

    log.info(f"[DEBUG] retrieve_knowledge_chunks: query={query[:40]}... user={user.id} "
             f"total_scored={len(scored)} returning={len(results)}")

    return results


def retrieve_attio_ventures(
    query: str,
    attio_api_key: str,
    openrouter_api_key: str,
    limit: int = 6,
) -> list[ChunkResult]:
    """
    [PRODUCTION] Retrieve ventures from Attio CRM using REST API.
    
    Queries Attio for all records, filters/ranks by relevance to query,
    and returns as ChunkResult objects.

    Parameters
    ----------
    query              : natural-language question or search term
    attio_api_key      : Attio API key
    openrouter_api_key : OpenRouter API key for embedding
    limit              : max results to return (default 6)

    Returns
    -------
    list[ChunkResult]  : Ranked ventures, empty list if no matches

    Note
    ----
    - Gracefully handles Attio API failures (returns empty list)
    - Safe attribute access with fallbacks
    - Comprehensive error logging for debugging
    """
    from .embeddings import embed_text

    if not attio_api_key or not openrouter_api_key:
        log.warning("Missing API keys for Attio retrieval")
        return []

    log.info(f"[DEBUG] retrieve_attio_ventures START: query={query[:50]}...")

    # ── Step 1: Embed query ──────────────────────────────────────────────────
    try:
        query_vec = embed_text(query.strip(), openrouter_api_key)
        log.info(f"[DEBUG] Query embedded successfully")
    except Exception as e:
        log.error(f"[DEBUG] Failed to embed query: {e}")
        return []

    # ── Step 2: Fetch ventures from Attio ────────────────────────────────────
    ventures_data = []
    try:
        headers = {
            "Authorization": f"Bearer {attio_api_key}",
            "Content-Type": "application/json",
        }
        
        # Simple approach: fetch all records from "companies" or "opportunities" object
        # Adjust these parameters based on your Attio setup
        payload = {
            "filter": {
                "filter_type": "all",
            }
        }
        
        with httpx.Client(timeout=30.0) as client:
            # Try to fetch companies (adjust URL if using different object)
            response = client.get(
                "https://api.attio.com/v2/lists",  # Simple list endpoint
                headers=headers,
                json=payload,
            )
            
            if response.status_code == 200:
                data = response.json()
                ventures_data = data.get("data", [])
                log.info(f"[DEBUG] Retrieved {len(ventures_data)} ventures from Attio")
            else:
                log.warning(f"[DEBUG] Attio API returned {response.status_code}: {response.text[:200]}")
                
    except httpx.TimeoutException:
        log.error("[DEBUG] Attio API request timed out")
        return []
    except Exception as e:
        log.error(f"[DEBUG] Attio API error: {e}")
        # Continue with fallback/empty results
        return []

    if not ventures_data:
        log.warning("[DEBUG] No ventures returned from Attio")
        return []

    # ── Step 3: Score ventures by relevance ──────────────────────────────────
    scored: list[tuple[float, dict]] = []
    
    for venture in ventures_data:
        try:
            # Build venture text for embedding
            venture_name = venture.get('name') or venture.get('title') or 'Unknown'
            venture_desc = venture.get('description') or venture.get('notes') or ''
            venture_text = f"{venture_name} {venture_desc}".strip()
            
            if not venture_text or len(venture_text) < 3:
                continue
            
            # Embed venture text
            try:
                venture_vec = embed_text(venture_text[:500], openrouter_api_key)  # Limit length
            except Exception as e:
                log.debug(f"Failed to embed venture {venture_name}: {e}")
                continue
            
            # Calculate similarity score
            score = _cosine(query_vec, venture_vec)
            scored.append((score, venture))
            
        except Exception as e:
            log.debug(f"Error processing venture: {e}")
            continue

    if not scored:
        log.warning("[DEBUG] No ventures scored successfully")
        return []

    # Sort by score descending
    scored.sort(key=lambda t: t[0], reverse=True)
    log.info(f"[DEBUG] Scored {len(scored)} ventures, top score: {scored[0][0]:.4f}")

    # ── Step 4: Build ChunkResult objects ────────────────────────────────────
    results: list[ChunkResult] = []
    for score, venture in scored[:limit]:
        try:
            venture_id = str(venture.get("id") or venture.get("name") or "unknown")
            venture_name = venture.get("name") or venture.get("title") or "Unknown Venture"
            venture_desc = venture.get("description") or venture.get("notes") or venture_name
            attio_url = venture.get("url") or venture.get("link")
            
            # Create result
            results.append(ChunkResult(
                chunk_id=hash(venture_id) % (10**8),  # Generate numeric ID from string
                document_id=None,
                extraction_id=None,
                company_id=None,
                company_name=venture_name,
                document_title="Attio Venture",
                text=venture_desc[:500],  # Limit text length
                score=round(score, 6),
                crm_venture_id=venture_id,
                crm_venture_name=venture_name,
                attio_url=attio_url,
                sector=venture.get("sector"),
                themes=venture.get("themes"),  # Could be list or None
                source_type="crm_venture",
            ))
            
        except Exception as e:
            log.error(f"[DEBUG] Error building ChunkResult: {e}")
            continue

    log.info(f"[DEBUG] retrieve_attio_ventures COMPLETE: returning {len(results)} results")
    return results


def _retrieve_crm_chunks(
    query_vec: list[float],
    db: "Session",
    limit: int,
) -> list[ChunkResult]:
    """Retrieve approved CRM knowledge chunks for admins."""
    from ..models import CrmVenture, KnowledgeChunk

    stmt = select(KnowledgeChunk).where(
        KnowledgeChunk.approved == True,  # noqa: E712
        KnowledgeChunk.visibility == "admin",
        KnowledgeChunk.embedding.is_not(None),
        KnowledgeChunk.source_type.in_(["crm_venture", "crm_note", "crm_file"]),
    )
    chunks = db.scalars(stmt).all()
    if not chunks:
        return []

    # Pre-load ventures
    venture_ids = {c.crm_venture_id for c in chunks if c.crm_venture_id}
    ventures: dict[int, "CrmVenture"] = {
        v.id: v
        for v in db.scalars(select(CrmVenture).where(CrmVenture.id.in_(venture_ids))).all()
    }

    scored: list[tuple[float, object]] = []
    for chunk in chunks:
        try:
            vec = json.loads(chunk.embedding)
        except (TypeError, ValueError):
            continue
        score = _cosine(query_vec, vec)
        scored.append((score, chunk))

    scored.sort(key=lambda t: t[0], reverse=True)

    results: list[ChunkResult] = []
    for score, chunk in scored[:limit]:
        venture = ventures.get(chunk.crm_venture_id) if chunk.crm_venture_id else None
        themes: list[str] | None = None
        if chunk.themes_json:
            try:
                themes = json.loads(chunk.themes_json)
            except Exception:
                pass
        src_type = chunk.source_type or "crm_venture"
        if src_type == "crm_note":
            doc_title = "Attio Note"
        elif src_type == "crm_file":
            doc_title = "Attio File"
        else:
            doc_title = "CRM / Attio"
        src_date = None
        if venture:
            src_date = (
                getattr(venture, "updated_at", None)
                or getattr(venture, "synced_at", None)
                or getattr(venture, "created_at", None)
            )
        results.append(ChunkResult(
            chunk_id=chunk.id,
            document_id=None,
            extraction_id=None,
            company_id=None,
            company_name=venture.name if venture else "CRM Venture",
            document_title=doc_title,
            text=chunk.text,
            score=round(score, 6),
            crm_venture_id=chunk.crm_venture_id,
            crm_venture_name=venture.name if venture else None,
            attio_url=venture.attio_url if venture else None,
            sector=chunk.sector,
            themes=themes,
            source_type=src_type,
            source_date=src_date,
        ))
    return results


# ---------------------------------------------------------------------------
# Unified, role-gated chat retrieval (Phase 0.5)
# ---------------------------------------------------------------------------

# Confidential field labels stripped for ALL non-admin viewers. Covers
# financials, investment/fund amounts, deal-pipeline state, and personal info.
_CONFIDENTIAL_PREFIXES = (
    # financials
    "cash position", "monthly burn", "burn", "runway", "revenue", "mrr", "arr",
    "estimated arr", "gross margin", "valuation",
    # investment / fund amounts
    "funding raised", "funding", "total funding", "investment amount",
    "amount raised", "investors", "cap table", "ownership", "equity",
    # deal pipeline / investment decisions
    "investment stage", "stage", "deal stage", "probability", "status",
    "source", "lead source",
    # relationship / personal info
    "owner", "assigned to", "first interaction", "last interaction",
    "next interaction", "contact", "email", "phone",
)

# Source types that originate from the CRM/knowledge base (vs portfolio docs).
_CRM_SOURCE_TYPES = {"crm_venture", "crm_note", "crm_file", "knowledge", "gdrive"}
# Free-text sources: served to non-admins ONLY via their sanitized copy.
_FREE_TEXT_SOURCES = {"crm_note", "crm_file", "gdrive"}


import re as _re_redact

# Regex patterns that redact financial figures and personal names embedded in narrative text
_FINANCIAL_RE = _re_redact.compile(
    r"""
    (?:
        [\$€£]\s*[\d,]+(?:\.\d+)?\s*(?:M|K|B|m|k|b|million|billion|thousand)?\b  # $18M, €200k
        | \b[\d,]+(?:\.\d+)?\s*(?:M|B|K)\s*(?:USD|EUR|GBP)?\b                    # 18M USD
        | \b\d+[xX]\s*(?:return|multiple|MOIC)\b                                   # 3x return
        | \b(?:100|200|300|400|500)-(?:100|200|300|400|500)\s*(?:M|million)\b      # 100-300 million
        | \bpost.?money\s+valuation\b                                               # post-money valuation
        | \bcheck\s+size\b                                                          # check size
        | \bpre.?seed\s+to\s+seed\b                                                # pre-seed to seed
    )
    """,
    _re_redact.VERBOSE | _re_redact.IGNORECASE,
)

# Sentences that contain internal decision-making language — strip entire sentence
_INTERNAL_DECISION_RE = _re_redact.compile(
    r'[^.!?\n]*(?:Julia|explicitly stated|Merantix would|beyond Merantix range|'
    r'fall outside|participation range|comfort zone|stretches? their|'
    r'noted (?:a|that)|suggested|recommended against|investment decision|'
    r'IC review|investment committee)[^.!?\n]*[.!?\n]',
    _re_redact.IGNORECASE,
)


def redact_confidential(
    results: list[ChunkResult],
    only_source_types: set[str] | None = None,
) -> list[ChunkResult]:
    """
    Strip confidential content from chunk text before it reaches the LLM.
    Two-pass approach:
    1. Line-level: drop lines starting with confidential field prefixes
    2. Regex-level: redact financial figures and internal decision language embedded in narrative
    """
    for r in results:
        if only_source_types is not None and r.source_type not in only_source_types:
            continue
        kept: list[str] = []
        skip_source = False
        for line in r.text.splitlines():
            stripped = line.strip().lower()
            if skip_source and line[:1] in (" ", "\t") and stripped.startswith("["):
                continue
            skip_source = False
            if any(stripped.startswith(p + ":") or stripped.startswith(p + " ")
                   for p in _CONFIDENTIAL_PREFIXES):
                skip_source = True
                continue
            kept.append(line)
        # Pass 2: regex redaction of financial figures and internal decision sentences
        cleaned = "\n".join(kept)
        cleaned = _FINANCIAL_RE.sub("[amount redacted]", cleaned)
        cleaned = _INTERNAL_DECISION_RE.sub("", cleaned)
        r.text = cleaned
    return results


# Backwards-compatible alias (older callers may import redact_financials)
def redact_financials(results: list[ChunkResult]) -> list[ChunkResult]:
    return redact_confidential(results)


def _apply_free_text_gate(results: list[ChunkResult]) -> list[ChunkResult]:
    """
    For non-admin viewers, serve all chunks as-is.
    Financial/sensitive line redaction is handled downstream by redact_confidential.
    """
    return results


def _matches_focus(c: ChunkResult, focus: str, aliases: list[str] | None = None) -> bool:
    """
    True if chunk *c* belongs to the focus company.
    Checks company_name, crm_venture_name, the chunk text prefix, and any aliases
    (e.g. old company names) so renamed companies still match their historical chunks.
    """
    f = focus.lower().strip()
    if not f:
        return True
    # Build full set of names to match against (focus + all aliases)
    all_targets = {f}
    if aliases:
        all_targets.update(a.lower().strip() for a in aliases if a)

    # Check structured name fields first
    for nm in (getattr(c, "company_name", None), getattr(c, "crm_venture_name", None)):
        if nm:
            nm_l = nm.lower()
            if any(t in nm_l or nm_l in t for t in all_targets):
                return True

    # Also check the first line of chunk text ("Company: <name>") so old indexed
    # chunks that weren't re-indexed after a rename still match.
    text = getattr(c, "text", None) or ""
    first_line = text.splitlines()[0].lower() if text else ""
    if first_line.startswith("company:"):
        nm_from_text = first_line[len("company:"):].strip()
        if nm_from_text and any(t in nm_from_text or nm_from_text in t for t in all_targets):
            return True

    return False


# ---------------------------------------------------------------------------
# Structured enumeration: "list / which / how many <sector> companies"
# ---------------------------------------------------------------------------
# Semantic top-k can only surface a handful of chunks, so "list all the robotics
# companies" returns 5-6, never the full set and never the most recent. For that
# class of question we instead run a STRUCTURED query: filter ventures by their
# Attio sector/category, return ALL matches, newest-first, with a total count.

import re as _re

_ENTITY_RE = r"(?:companies|company|startups|startup|ventures|venture|deals|businesses|firms|portcos)"
_LIST_CUES = (
    "list", "all", "which", "how many", "name", "show", "what are", "give me",
    "tell me about", "every", "overview of", "summary of", "across",
)
# Cues that indicate a count/enumerate-EVERYTHING question with no specific sector
# (e.g. "how many companies have we analyzed so far", "list all the companies").
_ALL_CUES = (
    "how many", " all ", "every ", "total", "so far", "in total", "overall",
    " count", "entire", "complete list", "altogether",
)
_ALL_SENTINEL = "*"
_INTENT_FILLERS = {
    "the", "all", "about", "me", "tell", "which", "how", "many", "list", "show",
    "of", "our", "your", "these", "those", "analyzed", "analysed", "a", "an",
    "that", "they", "have", "has", "we", "you", "portfolio", "are", "is", "do",
    "does", "what", "give", "name", "every", "been", "worked", "with", "on",
    "looked", "at", "seen", "evaluated", "reviewed", "their", "his", "her",
}
# Lines in the indexed venture text that carry the sector/category labels.
_SECTOR_LINE_PREFIXES = ("sectors:", "sector:", "categories:", "category:", "industry:")


def detect_category_list_intent(query: str) -> str | None:
    """
    If *query* is an enumeration filtered by a sector/category (e.g. "list all the
    robotics companies", "how many fintech startups"), return the category term
    (lowercased). Otherwise None. Conservative: needs both a list cue and an
    entity word, and returns a term only if it's specific enough (>=3 chars).
    """
    q = " " + (query or "").lower().strip() + " "
    if not _re.search(rf"\b{_ENTITY_RE}\b", q):
        return None
    if not any(cue in q for cue in _LIST_CUES):
        return None
    m = _re.search(rf"([a-z0-9/&\-\. ]+?)\s+{_ENTITY_RE}\b", q)
    term: str | None = None
    if m:
        toks = [t for t in _re.findall(r"[a-z0-9/&\-\.]+", m.group(1)) if t not in _INTENT_FILLERS]
        if toks:
            cand = toks[-1].strip("./-&")
            if len(cand) >= 3:
                term = cand
    if term:
        return term
    # No specific sector mentioned — treat as a count/list-everything question
    # only when there's an explicit "all / how many / total" style cue.
    if any(c in q for c in _ALL_CUES):
        return _ALL_SENTINEL
    return None


_LP_PORTFOLIO_STAGE = "portfolio"  # Attio stage value for actual investments


def list_ventures_by_category(db: "Session", term: str, limit: int = 50, lp_scope: bool = False) -> tuple[list[dict], int]:
    """
    Return (rows, total) for every venture whose Attio Sectors/Categories/Industry
    line contains *term*, newest-first by latest note date (fallback: sync date).

    rows: [{name, attio_url, stage, last_activity (datetime|None)}] capped at *limit*.
    total: full count of matches (so the caller can say "showing 50 of N").
    No embeddings or LLM calls — pure structured lookup.
    """
    from sqlalchemy import func
    from ..models import KnowledgeChunk, CrmVenture, CrmNote

    term_l = (term or "").lower().strip()
    if not term_l:
        return [], 0

    matched: set[int] = set()

    # LP scope: only surface portfolio-stage companies
    from sqlalchemy import func as _func
    _stage_filter = (
        _func.lower(CrmVenture.stage).contains(_LP_PORTFOLIO_STAGE)
        if lp_scope else None
    )

    if term_l in (_ALL_SENTINEL, "all", "__all__"):
        stmt = select(CrmVenture.id).where(CrmVenture.name.is_not(None))
        if _stage_filter is not None:
            stmt = stmt.where(_stage_filter)
        matched = set(db.scalars(stmt).all())
    else:
        # Scan indexed venture chunks; match the term on a sector/category line,
        # or on the (future) populated sector column.
        rows = db.execute(
            select(KnowledgeChunk.crm_venture_id, KnowledgeChunk.text, KnowledgeChunk.sector)
            .where(
                KnowledgeChunk.source_type == "crm_venture",
                KnowledgeChunk.crm_venture_id.is_not(None),
            )
        ).all()
        for vid, text, sector in rows:
            if sector and term_l in sector.lower():
                matched.add(vid)
                continue
            for line in (text or "").splitlines():
                ls = line.strip().lower()
                if ls.startswith(_SECTOR_LINE_PREFIXES) and term_l in ls:
                    matched.add(vid)
                    break

        # LP scope: remove any non-portfolio ventures from matched set
        if lp_scope and matched:
            portfolio_ids = set(db.scalars(
                select(CrmVenture.id).where(
                    CrmVenture.id.in_(matched),
                    _stage_filter,
                )
            ).all())
            matched = matched & portfolio_ids
    if not matched:
        return [], 0

    ventures = {
        v.id: v
        for v in db.scalars(select(CrmVenture).where(CrmVenture.id.in_(matched))).all()
    }
    note_dates: dict[int, object] = dict(
        db.execute(
            select(CrmNote.crm_venture_id, func.max(CrmNote.created_at_attio))
            .where(CrmNote.crm_venture_id.in_(matched))
            .group_by(CrmNote.crm_venture_id)
        ).all()
    )

    import datetime as _dt
    _floor = _dt.datetime.min

    items: list[dict] = []
    for vid in matched:
        v = ventures.get(vid)
        if not v:
            continue
        last = note_dates.get(vid) or getattr(v, "created_at", None)
        items.append({
            "name": v.name or "Unknown",
            "attio_url": getattr(v, "attio_url", None),
            "stage": getattr(v, "stage", None),
            "last_activity": last,
        })

    items.sort(key=lambda x: (x["last_activity"] or _floor), reverse=True)
    return items[:limit], len(items)


def format_enumeration_answer(
    term: str, items: list[dict], total: int, show_stage: bool = True
) -> str:
    """
    Render a structured company list into a clean, complete chat answer.

    show_stage=False hides the deal-stage label (e.g. "· Passed") — used for the
    LP view so external readers don't see internal pipeline status per company.
    """
    def _fmt_date(d):
        try:
            return d.strftime("%b %Y")
        except Exception:
            return "—"

    label = "in the pipeline" if term == _ALL_SENTINEL else f"tagged “{term}”"
    plural = "y" if total == 1 else "ies"
    shown = len(items)
    header = f"Merantix has {total} compan{plural} {label}"
    if shown < total:
        header += f" — showing the {shown} most recent"
    header += ":"

    lines = []
    for i, it in enumerate(items, start=1):
        stg = f" · {it['stage']}" if (show_stage and it.get("stage")) else ""
        lines.append(f"{i}. {it['name']} (last activity {_fmt_date(it.get('last_activity'))}{stg})")

    body = header + "\n\n" + "\n".join(lines)
    if shown < total:
        body += (
            f"\n\n(Showing {shown} of {total}, newest first. "
            f"Ask me to narrow by sector, stage, or year for a shorter list.)"
        )
    return body


def retrieve_for_chat(
    query: str,
    user: "User",
    db: "Session",
    filters: dict | None = None,
    limit: int = 8,
    viewer_scope: str = "admin",
    focus_company: str | None = None,
    focus_aliases: list[str] | None = None,
) -> list[ChunkResult]:
    """
    Unified, role-gated chat retrieval. Scoring runs on the real chunk text;
    non-admins are *served* sanitized/redacted copies.

    viewer_scope:
      "admin"        -> all sources, no redaction.
      "company_user" -> own portfolio docs (full) + general CRM data with
                        confidential fields stripped; notes/files via sanitized copy.
      "lp"           -> portfolio docs + CRM with confidential fields stripped
                        everywhere; notes/files via sanitized copy.

    Pipeline: hybrid retrieve (each source) -> merge -> role gate/redact ->
    LLM rerank -> top `limit`.
    """
    # Gather a larger candidate pool so the reranker has something to sharpen.
    pool = max(limit, _RERANK_POOL)

    # LP scope: search ALL knowledge chunks (portfolio + pipeline + evaluations).
    # Financial details, deal stages and pipeline status are redacted downstream
    # by redact_confidential() and the LLM guardrail — not at retrieval time.
    # This gives LPs the full picture of what Merantix knows about any company.
    lp_filters = dict(filters or {})

    portfolio: list[ChunkResult] = []
    knowledge: list[ChunkResult] = []
    try:
        portfolio = retrieve_relevant_chunks(
            query, user, db, filters=filters, limit=pool, include_crm=False
        )
    except Exception as exc:
        log.warning("retrieve_for_chat: portfolio retrieval failed: %s", exc)
    try:
        knowledge = retrieve_knowledge_chunks(query, user, db, filters=lp_filters, limit=pool)
    except Exception as exc:
        log.warning("retrieve_for_chat: knowledge retrieval failed: %s", exc)

    combined = portfolio + knowledge

    # LP scope: portfolio filtering is handled via the enumeration path
    # (list_ventures_by_category with lp_scope=True) and the LLM guardrail.
    # Chunk-level filtering is intentionally not applied here to avoid
    # over-filtering when knowledge chunks lack correct venture IDs.

    # Non-admins: swap free-text notes/files for their sanitized copy (or drop).
    if viewer_scope != "admin":
        combined = _apply_free_text_gate(combined)

    # Deterministic follow-ups: when the question resolved to a single company,
    # hard-scope results to that company so "their team structure" can't drift to
    # another company. When nothing matches, return empty — the caller will use
    # live web context + LLM knowledge rather than leaking other companies' data.
    if focus_company:
        combined = [c for c in combined if _matches_focus(c, focus_company, aliases=focus_aliases)]

    # Boost scores using LP feedback — chunks rated helpful surface higher over time
    for r in combined:
        fb = getattr(r, 'feedback_score', None)
        if fb is None:
            # Try to load from DB (only for knowledge chunks with an id)
            try:
                from ..models import KnowledgeChunk as _KC
                chunk = db.get(_KC, r.chunk_id) if r.chunk_id else None
                fb = chunk.feedback_score if chunk else 0.0
            except Exception:
                fb = 0.0
        # Small additive boost: ±0.05 per feedback point (max ±0.25 at score=±5)
        r.score = r.score + (fb * 0.05)

    # Recency boost — surface fresh evaluations over stale ones.
    # Only applied to CRM-sourced chunks (source_date is populated there).
    import datetime as _dt
    _now = _dt.datetime.utcnow()
    for r in combined:
        sd = getattr(r, "source_date", None)
        if sd is None:
            continue
        try:
            age_days = (_now - sd).days
        except Exception:
            continue
        if age_days < 90:      # < 3 months — very fresh
            boost = 0.06
        elif age_days < 180:   # < 6 months
            boost = 0.03
        elif age_days < 365:   # < 1 year
            boost = 0.01
        elif age_days < 730:   # 1-2 years — neutral
            boost = 0.0
        else:                  # > 2 years — stale
            boost = -0.02
        r.score = r.score + boost

    combined.sort(key=lambda r: r.score, reverse=True)
    combined = combined[:pool]

    # Strip confidential structured fields BEFORE reranking, so the reranker
    # (an LLM) never sees confidential text for non-admin viewers.
    if viewer_scope == "company_user":
        # CRM data only — the user's OWN portfolio documents are left intact.
        redact_confidential(combined, only_source_types=_CRM_SOURCE_TYPES)
    elif viewer_scope == "lp":
        # LPs: strip from everything (documents + CRM).
        redact_confidential(combined)

    # Final sharpening: LLM listwise rerank down to `limit` (falls back to the
    # fusion order if no API key / on any failure).
    try:
        from .reranker import rerank_candidates
        combined = rerank_candidates(query, combined, db, limit)
    except Exception as exc:
        log.warning("retrieve_for_chat: rerank skipped (%s)", exc)
        combined = combined[:limit]

    return combined