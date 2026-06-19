"""Reporting tracker: period generation and status resolution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Company, CompanyReportingSettings, Document, DocumentCategory,
    ExtractionJobStatus, ExtractionStatus, ReportingFrequency,
)


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def _current_period_monthly() -> tuple[int, int]:
    today = date.today()
    return today.year, today.month


def _current_period_quarterly() -> tuple[int, int]:
    today = date.today()
    return today.year, (today.month - 1) // 3 + 1


def _month_periods(start: date, end_year: int, end_month: int) -> list[tuple[int, int]]:
    """All (year, month) periods from start up to and including (end_year, end_month)."""
    periods = []
    y, m = start.year, start.month
    while (y, m) <= (end_year, end_month):
        periods.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return periods


def _quarter_periods(start: date, end_year: int, end_q: int) -> list[tuple[int, int]]:
    """All (year, quarter) periods from start up to and including (end_year, end_q)."""
    start_q = (start.month - 1) // 3 + 1
    periods = []
    y, q = start.year, start_q
    while (y, q) <= (end_year, end_q):
        periods.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return periods


# ---------------------------------------------------------------------------
# Status resolution
# ---------------------------------------------------------------------------

def _doc_status(doc: Document) -> str:
    """Map a document's state to a display status string."""
    from ..models import ReviewStatus
    if doc.review_status == ReviewStatus.approved:
        return "approved"
    if doc.review_status == ReviewStatus.rejected:
        return "rejected"
    # Check extraction
    ex_status = doc.extraction_status
    if ex_status == ExtractionStatus.failed:
        return "failed"
    if ex_status in (ExtractionStatus.extracted,):
        return "pending_review"
    if ex_status == ExtractionStatus.complete:
        return "text_extracted"
    return "uploaded"


STATUS_ORDER = {
    "missing": 0,
    "uploaded": 1,
    "text_extracted": 2,
    "pending_review": 3,
    "approved": 4,
    "rejected": 5,
    "failed": 6,
    "not_required": 7,
}

STATUS_BADGE = {
    "missing": ("badge-error", "Missing"),
    "uploaded": ("badge-info", "Uploaded"),
    "text_extracted": ("badge-info", "Text extracted"),
    "pending_review": ("badge-warn", "Pending review"),
    "approved": ("badge-success", "Approved"),
    "rejected": ("badge-error", "Rejected"),
    "failed": ("badge-error", "Failed"),
    "not_required": ("badge-neutral", "Not required"),
}


# ---------------------------------------------------------------------------
# Tracker row dataclass
# ---------------------------------------------------------------------------

@dataclass
class TrackerRow:
    company_id: int
    company_name: str
    category: str           # "monthly_reporting" | "quarterly_reporting"
    year: int
    period_label: str       # "Jan 2024" | "Q1 2024"
    due_date: Optional[date]
    status: str
    document: Optional[Document]

    @property
    def status_badge(self) -> tuple[str, str]:
        return STATUS_BADGE.get(self.status, ("badge-neutral", self.status))


# ---------------------------------------------------------------------------
# Build tracker rows for one company
# ---------------------------------------------------------------------------

def _due_date(year: int, month: int, day_due: int | None) -> date | None:
    if not day_due:
        return None
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day_due, last_day))


def _quarterly_due(year: int, quarter: int, day_due: int | None) -> date | None:
    if not day_due:
        return None
    # Due in the month after the quarter ends
    end_month = quarter * 3
    due_month = end_month + 1
    due_year = year
    if due_month > 12:
        due_month = 1
        due_year += 1
    import calendar
    last_day = calendar.monthrange(due_year, due_month)[1]
    return date(due_year, due_month, min(day_due, last_day))


MONTH_NAMES = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]


def build_rows_for_company(
    company: Company,
    settings: CompanyReportingSettings,
    db: Session,
) -> list[TrackerRow]:
    rows: list[TrackerRow] = []
    freq = settings.reporting_frequency

    if freq == ReportingFrequency.none or settings.reporting_start_date is None:
        return rows

    # Load all regular reporting documents for this company
    docs: list[Document] = list(db.scalars(
        select(Document).where(
            Document.company_id == company.id,
            Document.is_regular_reporting == True,  # noqa: E712
        )
    ).all())

    if freq == ReportingFrequency.monthly:
        cat = DocumentCategory.monthly_reporting
        cur_y, cur_m = _current_period_monthly()
        periods = _month_periods(settings.reporting_start_date, cur_y, cur_m)

        # Build lookup by (year, month)
        doc_map: dict[tuple[int, int], Document] = {}
        for d in docs:
            if d.document_category == cat and d.reporting_year and d.reporting_month:
                doc_map[(d.reporting_year, d.reporting_month)] = d

        for y, m in periods:
            doc = doc_map.get((y, m))
            rows.append(TrackerRow(
                company_id=company.id,
                company_name=company.name,
                category=cat.value,
                year=y,
                period_label=f"{MONTH_NAMES[m]} {y}",
                due_date=_due_date(y, m, settings.reporting_day_due),
                status=_doc_status(doc) if doc else "missing",
                document=doc,
            ))

    elif freq == ReportingFrequency.quarterly:
        cat = DocumentCategory.quarterly_reporting
        cur_y, cur_q = _current_period_quarterly()
        periods = _quarter_periods(settings.reporting_start_date, cur_y, cur_q)

        doc_map2: dict[tuple[int, int], Document] = {}
        for d in docs:
            if d.document_category == cat and d.reporting_year and d.reporting_quarter:
                doc_map2[(d.reporting_year, d.reporting_quarter)] = d

        for y, q in periods:
            doc = doc_map2.get((y, q))
            rows.append(TrackerRow(
                company_id=company.id,
                company_name=company.name,
                category=cat.value,
                year=y,
                period_label=f"Q{q} {y}",
                due_date=_quarterly_due(y, q, settings.reporting_day_due),
                status=_doc_status(doc) if doc else "missing",
                document=doc,
            ))

    return rows


# ---------------------------------------------------------------------------
# Irregular documents for a company
# ---------------------------------------------------------------------------

def get_irregular_docs(company_id: int, db: Session) -> list[Document]:
    return list(db.scalars(
        select(Document)
        .where(
            Document.company_id == company_id,
            Document.is_regular_reporting == False,  # noqa: E712
        )
        .order_by(Document.created_at.desc())
    ).all())


# ---------------------------------------------------------------------------
# Portfolio-wide tracker
# ---------------------------------------------------------------------------

def build_portfolio_tracker(db: Session) -> list[TrackerRow]:
    companies = list(db.scalars(select(Company).order_by(Company.name)).all())
    all_rows: list[TrackerRow] = []

    for company in companies:
        settings = db.scalar(
            select(CompanyReportingSettings).where(
                CompanyReportingSettings.company_id == company.id
            )
        )
        if not settings:
            continue
        rows = build_rows_for_company(company, settings, db)
        all_rows.extend(rows)

    return all_rows
