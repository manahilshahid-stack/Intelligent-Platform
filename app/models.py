from __future__ import annotations 

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer,
    LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class UploadStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    failed = "failed"
    complete = "complete"


class ExtractionStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    complete = "complete"       # text extracted successfully
    extracted = "extracted"     # LLM extraction done, pending admin review
    failed = "failed"           # text extraction or LLM call failed
    no_api_key = "no_api_key"   # text extracted but OpenRouter key not configured


class ReviewStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ExtractionJobStatus(str, enum.Enum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class ChunkType(str, enum.Enum):
    text = "text"
    kpi = "kpi"
    summary = "summary"
    approved_extraction = "approved_extraction"

class LPInterestArea(str, enum.Enum):
    """LP area of interest categories."""
    fintech = "fintech"
    healthcare = "healthcare"
    bioscience = "bioscience"
    robotics = "robotics"
    medicine = "medicine"
    drug_discovery = "drug_discovery"
    aerospace = "aerospace"
    climate_tech = "climate_tech"
    energy = "energy"
    ai_ml = "ai_ml"
    deeptech = "deeptech"
    defense = "defense"
    other = "other"


class LPLookingFor(str, enum.Enum):
    """What LPs are looking for."""
    deeptech_insights = "deeptech_insights"
    early_stage_coinvestment = "early_stage_coinvestment"
    quarterly_portfolio_updates = "quarterly_portfolio_updates"
    founder_operator_intros = "founder_operator_intros"
    merantix_insights = "merantix_insights"
    sector_benchmark = "sector_benchmark"
    theses = "theses"
    other = "other"

class DocumentCategory(str, enum.Enum):
    monthly_reporting = "monthly_reporting"
    quarterly_reporting = "quarterly_reporting"
    board_deck = "board_deck"
    pitch = "pitch"
    financing_deck = "financing_deck"
    ic_memo = "ic_memo"
    other = "other"


class ReportingFrequency(str, enum.Enum):
    monthly = "monthly"
    quarterly = "quarterly"
    none = "none"


class KpiFieldType(str, enum.Enum):
    text = "text"
    number = "number"
    currency = "currency"
    percentage = "percentage"
    date = "date"
    boolean = "boolean"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="company")
    documents: Mapped[list["Document"]] = relationship(back_populates="company", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_companies_slug", "slug"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="userrole"), nullable=False, default=UserRole.user
    )
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    company: Mapped[Company | None] = relationship(back_populates="users")
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(back_populates="uploaded_by")

    __table_args__ = (
        Index("ix_users_email", "email"),
        Index("ix_users_company_id", "company_id"),
    )


class UserSession(Base):
    """Server-side session store — one row per active browser session."""
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_user_sessions_token_hash", "token_hash"),
        Index("ix_user_sessions_user_id", "user_id"),
    )
class LPUser(Base):
    """Limited Partner user account with onboarding preferences."""
    __tablename__ = "lp_users"
 
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    organization: Mapped[str | None] = mapped_column(String(200))
    
    # Onboarding fields (can be NULL = skipped or not yet completed)
    interest_areas: Mapped[str | None] = mapped_column(Text)  # JSON array of LPInterestArea values
    looking_for: Mapped[str | None] = mapped_column(Text)     # JSON array of LPLookingFor values
    about_yourself: Mapped[str | None] = mapped_column(Text)  # Free text
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)
 
    sessions: Mapped[list["LPUserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    chat_sessions: Mapped[list["LPChatSession"]] = relationship(
        back_populates="lp_user", cascade="all, delete-orphan"
    )
 
    __table_args__ = (
        Index("ix_lp_users_email", "email"),
    )
 
 
class LPUserSession(Base):
    """Server-side session store for LP users."""
    __tablename__ = "lp_user_sessions"
 
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lp_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lp_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
 
    user: Mapped[LPUser] = relationship(back_populates="sessions")
 
    __table_args__ = (
        Index("ix_lp_user_sessions_token_hash", "token_hash"),
        Index("ix_lp_user_sessions_user_id", "lp_user_id"),
    )
 
 
class LPChatSession(Base):
    """Chat session for LP user."""
    __tablename__ = "lp_chat_sessions"
 
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lp_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lp_users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
 
    lp_user: Mapped[LPUser] = relationship(back_populates="chat_sessions")
    messages: Mapped[list["LPChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
 
    __table_args__ = (
        Index("ix_lp_chat_sessions_user_id", "lp_user_id"),
    )
 
 
class LPChatMessage(Base):
    """Individual chat message in LP session."""
    __tablename__ = "lp_chat_messages"
 
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lp_chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[str | None] = mapped_column(Text)  # JSON array of citation objects
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
 
    session: Mapped[LPChatSession] = relationship(back_populates="messages")
 
    __table_args__ = (
        Index("ix_lp_chat_messages_session_id", "session_id"),
    )

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    # Raw file bytes — set to NULL after extraction to keep Postgres lean
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    raw_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    upload_status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus, name="uploadstatus"),
        nullable=False,
        default=UploadStatus.uploaded,
    )
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        Enum(ExtractionStatus, name="extractionstatus"),
        nullable=False,
        default=ExtractionStatus.pending,
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="reviewstatus"),
        nullable=False,
        default=ReviewStatus.pending,
    )
    extraction_error: Mapped[str | None] = mapped_column(Text)

    # Categorisation & reporting period
    document_category: Mapped[DocumentCategory] = mapped_column(
        Enum(DocumentCategory, name="documentcategory"),
        nullable=False,
        default=DocumentCategory.other,
    )
    is_regular_reporting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reporting_period: Mapped[str | None] = mapped_column(String(20))   # display label e.g. "2024-Q1"
    reporting_year: Mapped[int | None] = mapped_column(Integer)
    reporting_month: Mapped[int | None] = mapped_column(Integer)       # 1-12
    reporting_quarter: Mapped[int | None] = mapped_column(Integer)     # 1-4

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    company: Mapped[Company] = relationship(back_populates="documents")
    uploaded_by: Mapped[User] = relationship(back_populates="documents")
    extractions: Mapped[list["Extraction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_documents_company_id", "company_id"),
        Index("ix_documents_uploaded_by_id", "uploaded_by_id"),
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_review_status", "review_status"),
    )


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    raw_llm_response: Mapped[str | None] = mapped_column(Text, deferred=True)
    extracted_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    corrected_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    status: Mapped[ExtractionJobStatus] = mapped_column(
        Enum(ExtractionJobStatus, name="extractionjobstatus"),
        nullable=False,
        default=ExtractionJobStatus.pending_review,
    )
    error: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="extractions")
    company: Mapped[Company] = relationship()
    reviewed_by: Mapped[User | None] = relationship()
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_extractions_document_id", "document_id"),
        Index("ix_extractions_company_id", "company_id"),
        Index("ix_extractions_status", "status"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    extraction_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("extractions.id", ondelete="SET NULL")
    )
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    chunk_type: Mapped[ChunkType] = mapped_column(
        Enum(ChunkType, name="chunktype"), nullable=False, default=ChunkType.text
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, deferred=True)
    # JSON-serialised list[float]; NULL until embedding is generated
    embedding: Mapped[str | None] = mapped_column(Text, deferred=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")
    extraction: Mapped[Extraction | None] = relationship(back_populates="chunks")
    company: Mapped[Company] = relationship()

    __table_args__ = (
        Index("ix_chunks_company_id", "company_id"),
        Index("ix_chunks_approved", "approved"),
        Index("ix_chunks_document_id", "document_id"),
    )


class CompanyReportingSettings(Base):
    __tablename__ = "company_reporting_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reporting_frequency: Mapped[ReportingFrequency] = mapped_column(
        Enum(ReportingFrequency, name="reportingfrequency"),
        nullable=False,
        default=ReportingFrequency.monthly,
    )
    reporting_start_date: Mapped[datetime | None] = mapped_column(Date)
    reporting_day_due: Mapped[int | None] = mapped_column(Integer)  # day-of-month, e.g. 15
    # JSON array of standard KPI keys excluded from this company's review/embeddings
    excluded_standard_kpis: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    company: Mapped[Company] = relationship()

    __table_args__ = (
        Index("ix_company_reporting_settings_company_id", "company_id"),
    )


class CompanyKpiField(Base):
    __tablename__ = "company_kpi_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    field_label: Mapped[str] = mapped_column(String(200), nullable=False)
    field_type: Mapped[KpiFieldType] = mapped_column(
        Enum(KpiFieldType, name="kpifieldtype"),
        nullable=False,
        default=KpiFieldType.text,
    )
    description: Mapped[str | None] = mapped_column(Text)
    extraction_hint: Mapped[str | None] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    company: Mapped[Company] = relationship()

    __table_args__ = (
        UniqueConstraint("company_id", "field_key", name="uq_company_kpi_field_key"),
        Index("ix_company_kpi_fields_company_id", "company_id"),
    )


class CrmSyncStatus(str, enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class CrmVenture(Base):
    """CRM records imported from Attio. Completely separate from portfolio companies."""
    __tablename__ = "crm_ventures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # List-entry identity (primary dedup key for list sync)
    attio_entry_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    attio_list_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Linked company record identity (object sync or resolved from list entry)
    attio_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Normalised fields
    name: Mapped[str | None] = mapped_column(String(500))
    website: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(String(200))
    sector: Mapped[str | None] = mapped_column(String(200))
    owner: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str | None] = mapped_column(String(200))
    attio_url: Mapped[str | None] = mapped_column(String(500))
    # Raw JSON blobs — deferred so they are NOT loaded on list queries
    raw_entry_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    raw_record_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    raw_attio_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_crm_ventures_attio_entry_id", "attio_entry_id"),
        Index("ix_crm_ventures_attio_record_id", "attio_record_id"),
        Index("ix_crm_ventures_name", "name"),
    )


class CrmSyncRun(Base):
    """Log of each manual Attio sync run."""
    __tablename__ = "crm_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False, default="attio_list")
    status: Mapped[CrmSyncStatus] = mapped_column(
        Enum(CrmSyncStatus, name="crmsyncstatus"), nullable=False, default=CrmSyncStatus.running
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    records_total: Mapped[int | None] = mapped_column(Integer)   # known after first fetch
    records_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_crm_sync_runs_started_at", "started_at"),
    )


class AppSetting(Base):
    """Single-row-per-key application configuration store."""
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_app_settings_key", "key"),
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = admin querying across all companies
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    user: Mapped[User] = relationship()
    company: Mapped[Company | None] = relationship()
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )

    __table_args__ = (
        Index("ix_chat_sessions_user_id", "user_id"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_chat_messages_session_id", "session_id"),
    )


# ---------------------------------------------------------------------------
# CRM Files (synced from Attio)
# ---------------------------------------------------------------------------

class CrmFileStatus(str, enum.Enum):
    pending          = "pending"
    text_extracted   = "text_extracted"
    unsupported      = "unsupported"
    failed           = "failed"


class CrmFile(Base):
    """A file attachment linked to an Attio CRM company record."""
    __tablename__ = "crm_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attio_file_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    crm_venture_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crm_ventures.id", ondelete="SET NULL"), nullable=True
    )
    attio_record_id: Mapped[str | None] = mapped_column(String(128))
    attio_entry_id: Mapped[str | None] = mapped_column(String(128))
    filename: Mapped[str | None] = mapped_column(String(512))
    file_type: Mapped[str | None] = mapped_column(String(64))    # extension: pdf, docx…
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(Integer)       # bytes
    download_url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    raw_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    extraction_status: Mapped[CrmFileStatus] = mapped_column(
        Enum(CrmFileStatus, name="crmfilestatus"),
        nullable=False, default=CrmFileStatus.pending,
    )
    extraction_error: Mapped[str | None] = mapped_column(Text, deferred=True)
    raw_file_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    crm_venture: Mapped["CrmVenture | None"] = relationship()

    __table_args__ = (
        Index("ix_crm_files_attio_file_id", "attio_file_id"),
        Index("ix_crm_files_crm_venture_id", "crm_venture_id"),
        Index("ix_crm_files_attio_record_id", "attio_record_id"),
        Index("ix_crm_files_extraction_status", "extraction_status"),
    )


# ---------------------------------------------------------------------------
# CRM Notes (synced from Attio)
# ---------------------------------------------------------------------------

class CrmNote(Base):
    """A note attached to a company record in Attio."""
    __tablename__ = "crm_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attio_note_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    crm_venture_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crm_ventures.id", ondelete="SET NULL"), nullable=True
    )
    attio_record_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(512))
    content_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    raw_note_json: Mapped[str | None] = mapped_column(Text, deferred=True)
    created_by: Mapped[str | None] = mapped_column(String(256))
    created_at_attio: Mapped[datetime | None] = mapped_column(DateTime)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    crm_venture: Mapped["CrmVenture | None"] = relationship()

    __table_args__ = (
        Index("ix_crm_notes_attio_note_id", "attio_note_id"),
        Index("ix_crm_notes_crm_venture_id", "crm_venture_id"),
        Index("ix_crm_notes_attio_record_id", "attio_record_id"),
    )


# ---------------------------------------------------------------------------
# External documents (Google Drive / Docs linked from the CRM)
# ---------------------------------------------------------------------------

class ExternalDocStatus(str, enum.Enum):
    pending = "pending"
    fetched = "fetched"
    no_access = "no_access"      # link found but not readable (private, no creds)
    unsupported = "unsupported"  # e.g. a Drive folder (needs API listing)
    failed = "failed"


class ExternalDocument(Base):
    """
    A document referenced by a LINK inside the CRM (Google Doc/Slides/Sheet/Drive
    file). Tracks fetch status + extracted text; the text is chunked into
    knowledge_chunks (source_type='gdrive') for retrieval.
    """
    __tablename__ = "external_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="gdrive")
    kind: Mapped[str | None] = mapped_column(String(32))      # doc | slides | sheet | drive_file | pub | folder
    file_id: Mapped[str | None] = mapped_column(String(256))
    crm_venture_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crm_ventures.id", ondelete="SET NULL"), nullable=True
    )
    source_ref: Mapped[str | None] = mapped_column(String(128))  # e.g. "note:123" / "venture:45"
    title: Mapped[str | None] = mapped_column(String(512))
    file_type: Mapped[str | None] = mapped_column(String(32))
    sha256: Mapped[str | None] = mapped_column(String(64))
    raw_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    status: Mapped[ExternalDocStatus] = mapped_column(
        Enum(ExternalDocStatus, name="externaldocstatus"),
        nullable=False, default=ExternalDocStatus.pending,
    )
    error: Mapped[str | None] = mapped_column(Text, deferred=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_external_documents_url", "url"),
        Index("ix_external_documents_crm_venture_id", "crm_venture_id"),
        Index("ix_external_documents_status", "status"),
    )


# ---------------------------------------------------------------------------
# Knowledge base (CRM ventures indexing)
# ---------------------------------------------------------------------------

class KnowledgeSource(Base):
    """One indexable unit — currently always a CRM venture."""
    __tablename__ = "knowledge_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="crm_venture")
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)   # crm_venture.id
    crm_venture_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crm_ventures.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(512))
    # visibility: "admin" = only admins can query; "all" = all users
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="admin")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    crm_venture: Mapped["CrmVenture | None"] = relationship()
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="knowledge_source",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_knowledge_sources_source_type_id", "source_type", "source_id"),
        Index("ix_knowledge_sources_crm_venture_id", "crm_venture_id"),
    )


class KnowledgeChunk(Base):
    """Embedded text chunk from a KnowledgeSource."""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    knowledge_source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    crm_venture_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crm_ventures.id", ondelete="CASCADE"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="crm_venture")
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, deferred=True)
    # Non-admin-safe rewrite (financials/personal/deal info removed). NULL until
    # generated for crm_note / crm_file chunks; non-admins are served this copy.
    sanitized_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    embedding: Mapped[str | None] = mapped_column(Text, deferred=True)   # JSON-encoded float list
    sector: Mapped[str | None] = mapped_column(String(256))
    themes_json: Mapped[str | None] = mapped_column(Text, deferred=True)  # JSON list of theme strings
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="admin")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    knowledge_source: Mapped[KnowledgeSource] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_knowledge_chunks_knowledge_source_id", "knowledge_source_id"),
        Index("ix_knowledge_chunks_crm_venture_id", "crm_venture_id"),
        Index("ix_knowledge_chunks_approved", "approved"),
    )
