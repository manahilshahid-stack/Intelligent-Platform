"""
Idempotent schema migrations.

Strategy:
  1. `Base.metadata.create_all` creates any table that does not yet exist.
     It never drops or alters existing tables, so it is always safe.
  2. `_ensure_columns` adds individual columns that may be missing from tables
     that were created by an earlier version of the schema.
  3. `_ensure_indexes` creates any missing indexes (IF NOT EXISTS).
  4. Enum types are created via `CREATE TYPE … IF NOT EXISTS` before the tables
     that reference them are touched.

Safe to run on every startup.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .database import Base, engine
from . import models  # noqa: F401  — registers all ORM classes with Base.metadata

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_postgres(conn) -> bool:
    return "postgresql" in str(conn.engine.url)


def _column_exists(conn, table: str, column: str) -> bool:
    if _is_postgres(conn):
        result = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
    else:
        # SQLite: PRAGMA table_info returns one row per column
        result = conn.execute(text(f"PRAGMA table_info({table})"))
        return any(row[1] == column for row in result.fetchall())
    return result.fetchone() is not None


def _table_exists(conn, table: str) -> bool:
    if _is_postgres(conn):
        result = conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
            {"t": table},
        )
        return result.fetchone() is not None
    else:
        result = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )
        return result.fetchone() is not None


def _enum_exists(conn, name: str) -> bool:
    if not _is_postgres(conn):
        return False
    result = conn.execute(
        text("SELECT 1 FROM pg_type WHERE typname = :n"),
        {"n": name},
    )
    return result.fetchone() is not None


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    if not _column_exists(conn, table, column):
        log.info("Adding column %s.%s", table, column)
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))


# ---------------------------------------------------------------------------
# Enum DDL (Postgres-only; SQLite auto-falls-through because _enum_exists
# will return False and we skip the CREATE TYPE statement there)
# ---------------------------------------------------------------------------

_ENUMS: dict[str, list[str]] = {
    "userrole": ["admin", "user"],
    "uploadstatus": ["uploaded", "processing", "failed", "complete"],
    "extractionstatus": ["pending", "running", "complete", "extracted", "failed", "no_api_key"],
    "reviewstatus": ["pending", "approved", "rejected"],
    "extractionjobstatus": ["pending_review", "approved", "rejected"],
    "chunktype": ["text", "kpi", "summary", "approved_extraction"],
    "documentcategory": [
        "monthly_reporting", "quarterly_reporting", "board_deck",
        "pitch", "financing_deck", "ic_memo", "other",
    ],
    "reportingfrequency": ["monthly", "quarterly", "none"],
    "kpifieldtype": ["text", "number", "currency", "percentage", "date", "boolean"],
    "crmsyncstatus": ["running", "success", "failed"],
    "crmfilestatus": ["pending", "text_extracted", "unsupported", "failed"],
    "externaldocstatus": ["pending", "fetched", "no_access", "unsupported", "failed"],
}


def _enum_value_exists(conn, type_name: str, value: str) -> bool:
    result = conn.execute(
        text(
            "SELECT 1 FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :t AND e.enumlabel = :v"
        ),
        {"t": type_name, "v": value},
    )
    return result.fetchone() is not None


def _ensure_enums(conn) -> None:
    """Create Postgres enum types that don't exist yet; add missing values to existing ones."""
    if not _is_postgres(conn):
        return
    for name, values in _ENUMS.items():
        if not _enum_exists(conn, name):
            quoted = ", ".join(f"'{v}'" for v in values)
            log.info("Creating enum type %s", name)
            conn.execute(text(f"CREATE TYPE {name} AS ENUM ({quoted})"))
        else:
            # Add any new values that were added to the definition after initial creation
            for value in values:
                if not _enum_value_exists(conn, name, value):
                    log.info("Adding value %r to enum %s", value, name)
                    conn.execute(text(f"ALTER TYPE {name} ADD VALUE IF NOT EXISTS '{value}'"))


# ---------------------------------------------------------------------------
# Column back-fills (add columns that newer schema versions introduced)
# ---------------------------------------------------------------------------

_COLUMN_ADDITIONS: list[tuple[str, str, str]] = [
    # (table, column, sql_type_definition)
    # users
    ("users", "name", "VARCHAR(200)"),
    ("users", "company_id", "INTEGER REFERENCES companies(id) ON DELETE SET NULL"),
    # documents
    ("documents", "extraction_error", "TEXT"),
    ("documents", "file_bytes", "BYTEA"),
    ("documents", "raw_text", "TEXT"),
    ("documents", "sha256", "VARCHAR(64)"),
    ("documents", "file_size", "INTEGER"),
    ("documents", "title", "VARCHAR(512)"),
    # extractions
    ("extractions", "corrected_json", "TEXT"),
    ("extractions", "raw_llm_response", "TEXT"),
    ("extractions", "error", "TEXT"),
    ("extractions", "reviewed_by_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("extractions", "reviewed_at", "TIMESTAMP"),
    # chunks
    ("chunks", "extraction_id", "INTEGER REFERENCES extractions(id) ON DELETE SET NULL"),
    ("chunks", "embedding", "TEXT"),
    # knowledge_chunks — non-admin-safe sanitized copy
    ("knowledge_chunks", "sanitized_text", "TEXT"),
    # chat
    ("chat_sessions", "company_id", "INTEGER REFERENCES companies(id) ON DELETE CASCADE"),
    ("chat_messages", "citations_json", "TEXT"),
    # company_reporting_settings — excluded standard KPIs
    ("company_reporting_settings", "excluded_standard_kpis", "TEXT"),
    # documents — reporting fields
    ("documents", "document_category", "documentcategory NOT NULL DEFAULT 'other'"),
    ("documents", "is_regular_reporting", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("documents", "reporting_period", "VARCHAR(20)"),
    ("documents", "reporting_year", "INTEGER"),
    ("documents", "reporting_month", "INTEGER"),
    ("documents", "reporting_quarter", "INTEGER"),
]


def _drop_constraint_if_exists(conn, table: str, constraint: str) -> None:
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = :t AND constraint_name = :c"
        ),
        {"t": table, "c": constraint},
    )
    if result.fetchone():
        log.info("Dropping constraint %s on %s", constraint, table)
        conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}"))


def _ensure_columns(conn) -> None:
    # Cross-DB additive columns: applied on BOTH SQLite and Postgres because an
    # existing local dev.db (created before this column) won't get it from
    # create_all. ADD COLUMN of a nullable TEXT is safe on both engines.
    if _table_exists(conn, "knowledge_chunks"):
        _add_column_if_missing(conn, "knowledge_chunks", "sanitized_text", "TEXT")

    # Column back-fills below use Postgres-specific DDL (BYTEA, enum FK types).
    # On SQLite (local dev), create_all already creates the full schema, so
    # this step is skipped — it is only needed when applying schema changes
    # to an existing Postgres database that predates a new column.
    if not _is_postgres(conn):
        return
    for table, column, defn in _COLUMN_ADDITIONS:
        if _table_exists(conn, table):
            _add_column_if_missing(conn, table, column, defn)

    # crm_ventures: drop old UNIQUE NOT NULL on attio_record_id, add new columns
    if _table_exists(conn, "crm_ventures"):
        _drop_constraint_if_exists(conn, "crm_ventures", "crm_ventures_attio_record_id_key")
        # make attio_record_id nullable if it isn't already
        try:
            conn.execute(text(
                "ALTER TABLE crm_ventures ALTER COLUMN attio_record_id DROP NOT NULL"
            ))
        except Exception:
            pass  # already nullable
        _add_column_if_missing(conn, "crm_ventures", "attio_entry_id", "VARCHAR(128) UNIQUE")
        _add_column_if_missing(conn, "crm_ventures", "attio_list_id", "VARCHAR(128)")
        _add_column_if_missing(conn, "crm_ventures", "raw_entry_json", "TEXT")
        _add_column_if_missing(conn, "crm_ventures", "raw_record_json", "TEXT")

    # crm_sync_runs: add sync_type, records_total, make counts non-nullable with defaults
    if _table_exists(conn, "crm_sync_runs"):
        _add_column_if_missing(conn, "crm_sync_runs", "records_total", "INTEGER")
        _add_column_if_missing(
            conn, "crm_sync_runs", "sync_type",
            "VARCHAR(50) NOT NULL DEFAULT 'attio_list'"
        )
        # Ensure records_seen/created/updated have defaults (they were nullable before)
        for col in ("records_seen", "records_created", "records_updated"):
            try:
                conn.execute(text(
                    f"ALTER TABLE crm_sync_runs ALTER COLUMN {col} SET DEFAULT 0"
                ))
                conn.execute(text(
                    f"UPDATE crm_sync_runs SET {col} = 0 WHERE {col} IS NULL"
                ))
            except Exception:
                pass

    # Ensure reporting enum columns use correct type (idempotent via _add_column_if_missing)
    # The enum types are guaranteed to exist at this point because _ensure_enums ran first.


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

_INDEXES: list[tuple[str, str, str]] = [
    # (index_name, table, column(s))
    ("ix_companies_slug", "companies", "slug"),
    ("ix_users_email", "users", "email"),
    ("ix_users_company_id", "users", "company_id"),
    ("ix_user_sessions_token_hash", "user_sessions", "token_hash"),
    ("ix_user_sessions_user_id", "user_sessions", "user_id"),
    ("ix_documents_company_id", "documents", "company_id"),
    ("ix_documents_uploaded_by_id", "documents", "uploaded_by_id"),
    ("ix_documents_sha256", "documents", "sha256"),
    ("ix_documents_review_status", "documents", "review_status"),
    ("ix_extractions_document_id", "extractions", "document_id"),
    ("ix_extractions_company_id", "extractions", "company_id"),
    ("ix_extractions_status", "extractions", "status"),
    ("ix_chunks_company_id", "chunks", "company_id"),
    ("ix_chunks_approved", "chunks", "approved"),
    ("ix_chunks_document_id", "chunks", "document_id"),
    ("ix_chat_sessions_user_id", "chat_sessions", "user_id"),
    ("ix_chat_messages_session_id", "chat_messages", "session_id"),
    ("ix_app_settings_key", "app_settings", "key"),
    ("ix_company_reporting_settings_company_id", "company_reporting_settings", "company_id"),
    ("ix_company_kpi_fields_company_id", "company_kpi_fields", "company_id"),
    ("ix_documents_company_category", "documents", "company_id, document_category"),
    ("ix_documents_reporting", "documents", "company_id, reporting_year, reporting_month, reporting_quarter"),
    ("ix_crm_ventures_attio_entry_id", "crm_ventures", "attio_entry_id"),
    ("ix_crm_ventures_attio_record_id", "crm_ventures", "attio_record_id"),
    ("ix_crm_ventures_name", "crm_ventures", "name"),
    ("ix_crm_sync_runs_started_at", "crm_sync_runs", "started_at"),
    ("ix_crm_files_attio_file_id", "crm_files", "attio_file_id"),
    ("ix_crm_files_crm_venture_id", "crm_files", "crm_venture_id"),
    ("ix_crm_files_attio_record_id", "crm_files", "attio_record_id"),
    ("ix_crm_files_extraction_status", "crm_files", "extraction_status"),
    ("ix_crm_notes_attio_note_id", "crm_notes", "attio_note_id"),
    ("ix_crm_notes_crm_venture_id", "crm_notes", "crm_venture_id"),
    ("ix_crm_notes_attio_record_id", "crm_notes", "attio_record_id"),
    ("ix_knowledge_sources_source_type_id", "knowledge_sources", "source_type, source_id"),
    ("ix_knowledge_sources_crm_venture_id", "knowledge_sources", "crm_venture_id"),
    ("ix_knowledge_chunks_knowledge_source_id", "knowledge_chunks", "knowledge_source_id"),
    ("ix_knowledge_chunks_crm_venture_id", "knowledge_chunks", "crm_venture_id"),
    ("ix_knowledge_chunks_approved", "knowledge_chunks", "approved"),
]


def _ensure_indexes(conn) -> None:
    for idx_name, table, columns in _INDEXES:
        if not _table_exists(conn, table):
            continue
        if _is_postgres(conn):
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({columns})")
            )
        else:
            exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"),
                {"n": idx_name},
            ).fetchone()
            if not exists:
                conn.execute(
                    text(f"CREATE INDEX {idx_name} ON {table} ({columns})")
                )


# ---------------------------------------------------------------------------
# pgvector setup (Postgres only, fully non-fatal)
# ---------------------------------------------------------------------------

# Embedding dimension for OpenRouter's text-embedding-3-small (default model).
_EMBEDDING_DIM = 1536


def _ensure_pgvector(bind: Engine) -> None:
    """
    Best-effort pgvector setup on Postgres. Adds a real `vector` column +
    HNSW index alongside the existing JSON `embedding` column (additive).

    Fully NON-FATAL: each statement runs in its own transaction inside a
    try/except, so if the `vector` extension is unavailable, an index type is
    unsupported, or anything else fails, we log a warning and the app keeps
    using the existing JSON-embedding / Python-cosine path. Never aborts startup.

    Does nothing on SQLite (local dev).
    """
    try:
        if "postgresql" not in str(bind.url):
            return
    except Exception:
        return

    statements = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_vec vector({_EMBEDDING_DIM})",
        f"ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_vec vector({_EMBEDDING_DIM})",
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_vec "
        "ON chunks USING hnsw (embedding_vec vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_vec "
        "ON knowledge_chunks USING hnsw (embedding_vec vector_cosine_ops)",
    ]
    for sql in statements:
        try:
            with bind.begin() as conn:
                conn.execute(text(sql))
        except Exception as exc:
            log.warning("pgvector setup step skipped — %s… (%s)", sql[:48], exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_migrations(bind: Engine = engine) -> None:
    """
    Run all migrations against the given engine.
    Called once at application startup before the first request is served.
    """
    log.info("Running schema migrations…")
    with bind.begin() as conn:
        # 1. Enum types must exist before create_all tries to use them
        _ensure_enums(conn)

    # 2. Create missing tables (never drops existing ones)
    Base.metadata.create_all(bind=bind)

    with bind.begin() as conn:
        # 3. Add any missing columns to pre-existing tables
        _ensure_columns(conn)
        # 4. Create any missing indexes
        _ensure_indexes(conn)

    # 5. Optional pgvector acceleration (Postgres only, non-fatal)
    _ensure_pgvector(bind)

    # 6. Feedback loop tables
    _ensure_feedback(bind)

    # 7. OTP email-verification columns
    _ensure_otp_columns(bind)

    log.info("Schema migrations complete.")


def _ensure_otp_columns(bind) -> None:
    """Add OTP email-verification columns to lp_users (idempotent)."""
    steps = [
        "ALTER TABLE lp_users ADD COLUMN IF NOT EXISTS avatar TEXT",
        "ALTER TABLE lp_users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE lp_users ADD COLUMN IF NOT EXISTS otp_code VARCHAR(6)",
        "ALTER TABLE lp_users ADD COLUMN IF NOT EXISTS otp_expires_at TIMESTAMP",
    ]
    try:
        with bind.begin() as conn:
            if not _is_postgres(conn):
                return  # SQLite: create_all already applies the full schema
            for sql in steps:
                try:
                    conn.execute(text(sql))
                except Exception as exc:
                    log.warning("OTP migration step skipped: %s", exc)
    except Exception as exc:
        log.warning("OTP migration failed: %s", exc)


def _ensure_feedback(bind) -> None:
    """Add feedback_score to knowledge_chunks and create lp_message_feedback table."""
    create_feedback_table = (
        "CREATE TABLE IF NOT EXISTS lp_message_feedback ("
        "id SERIAL PRIMARY KEY, "
        "lp_user_id INTEGER NOT NULL REFERENCES lp_users(id) ON DELETE CASCADE, "
        "message_id INTEGER NOT NULL REFERENCES lp_chat_messages(id) ON DELETE CASCADE, "
        "rating INTEGER NOT NULL, "
        "chunk_ids TEXT, "
        "created_at TIMESTAMP NOT NULL DEFAULT NOW(), "
        "CONSTRAINT uq_lp_feedback_per_message UNIQUE (lp_user_id, message_id)"
        ")"
    )
    steps = [
        "ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS feedback_score FLOAT NOT NULL DEFAULT 0.0",
        create_feedback_table,
        "CREATE INDEX IF NOT EXISTS ix_lp_message_feedback_message_id ON lp_message_feedback(message_id)",
        "CREATE INDEX IF NOT EXISTS ix_lp_message_feedback_lp_user_id ON lp_message_feedback(lp_user_id)",
    ]
    try:
        with bind.begin() as conn:
            for sql in steps:
                try:
                    conn.execute(text(sql))
                except Exception as exc:
                    log.warning("feedback migration step skipped: %s", exc)
    except Exception as exc:
        log.warning("feedback migration failed: %s", exc)
