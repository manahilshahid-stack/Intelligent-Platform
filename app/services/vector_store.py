"""
pgvector helpers (Phase 1, dual-mode).

This module isolates everything pgvector-specific. It is designed to be
COMPLETELY SAFE on SQLite and on Postgres instances where the `vector`
extension is not enabled: every function degrades to a no-op / False and the
caller falls back to the existing JSON-embedding + Python-cosine path.

Public API
----------
is_postgres(db)            -> bool
pgvector_available(db)     -> bool   # extension + column present (cached)
backfill_pgvector(db)      -> dict   # copy JSON embeddings -> vector column
vec_literal(vector)        -> str    # python list[float] -> '[..]' for ::vector
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Tables that carry an embedding_vec column.
_VECTOR_TABLES = ("chunks", "knowledge_chunks")

# Cache the availability check per-process so we don't probe on every query.
_AVAILABLE_CACHE: bool | None = None


def is_postgres(db: "Session") -> bool:
    try:
        return "postgresql" in str(db.get_bind().url)
    except Exception:
        return False


def pgvector_available(db: "Session", refresh: bool = False) -> bool:
    """
    True only if we're on Postgres AND the `vector` extension is installed AND
    the embedding_vec column exists on knowledge_chunks. Result is cached for
    the process lifetime (pass refresh=True to re-probe).
    """
    global _AVAILABLE_CACHE
    if _AVAILABLE_CACHE is not None and not refresh:
        return _AVAILABLE_CACHE

    available = False
    try:
        if is_postgres(db):
            ext = db.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).fetchone()
            col = db.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'knowledge_chunks' AND column_name = 'embedding_vec'"
                )
            ).fetchone()
            available = bool(ext) and bool(col)
    except Exception as exc:
        log.warning("pgvector_available probe failed (assuming unavailable): %s", exc)
        available = False

    _AVAILABLE_CACHE = available
    log.info("pgvector_available = %s", available)
    return available


def vec_literal(vector: list[float]) -> str:
    """Render a python float list as a pgvector text literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def vector_topk(
    db: "Session",
    table: str,
    query_vec: list[float],
    limit: int,
    extra_where: str = "",
    params: dict | None = None,
) -> list[tuple[int, float]]:
    """
    Return [(id, cosine_similarity)] for the top *limit* rows of *table*, ranked
    by pgvector cosine distance. Only call when pgvector_available() is True.

    *table* must be one of the known vector tables (guards against injection).
    *extra_where* is ANDed in and may use bound :params.
    """
    if table not in _VECTOR_TABLES:
        raise ValueError(f"vector_topk: unknown table {table!r}")

    where = "embedding_vec IS NOT NULL AND approved = TRUE"
    if extra_where:
        where += " AND (" + extra_where + ")"
    sql = text(
        f"SELECT id, 1 - (embedding_vec <=> CAST(:qvec AS vector)) AS score "
        f"FROM {table} WHERE {where} "
        f"ORDER BY embedding_vec <=> CAST(:qvec AS vector) LIMIT :limit"
    )
    p = {"qvec": vec_literal(query_vec), "limit": int(limit)}
    if params:
        p.update(params)
    rows = db.execute(sql, p).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def set_row_vector(db: "Session", table: str, row_id: int, vector: list[float]) -> None:
    """
    Populate embedding_vec for a single row (Postgres + pgvector only). Used on
    write so freshly-embedded chunks are immediately searchable via the ANN
    index. Non-fatal: no-ops on SQLite / when pgvector is unavailable.
    """
    if table not in _VECTOR_TABLES or not vector or not pgvector_available(db):
        return
    try:
        db.execute(
            text(f"UPDATE {table} SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            {"v": vec_literal(vector), "id": int(row_id)},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        log.warning("set_row_vector(%s, %s) failed: %s", table, row_id, exc)


def backfill_pgvector(db: "Session") -> dict:
    """
    Populate embedding_vec from the existing JSON `embedding` text column for
    rows that don't have it yet, on both chunks and knowledge_chunks.

    Uses Postgres' built-in text->vector cast (pgvector accepts the JSON-style
    "[a, b, c]" string). Non-fatal: per-table failures are logged and skipped.

    Returns {"chunks": N, "knowledge_chunks": M, "errors": E}.
    """
    result = {"chunks": 0, "knowledge_chunks": 0, "errors": 0}
    if not pgvector_available(db, refresh=True):
        log.warning("backfill_pgvector: pgvector not available; nothing to do.")
        result["error"] = "pgvector_unavailable"
        return result

    for table in _VECTOR_TABLES:
        try:
            res = db.execute(text(
                f"UPDATE {table} SET embedding_vec = embedding::vector "
                f"WHERE embedding IS NOT NULL AND embedding_vec IS NULL"
            ))
            db.commit()
            result[table] = res.rowcount or 0
            log.info("backfill_pgvector: %s updated %d rows", table, result[table])
        except Exception as exc:
            db.rollback()
            result["errors"] += 1
            log.warning("backfill_pgvector: %s failed: %s", table, exc)

    return result
