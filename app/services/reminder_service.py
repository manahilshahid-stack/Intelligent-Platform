"""
Quarterly report reminder engine.

Rules (per Manahil, Jul 2026):
  * Each portfolio company must upload its quarterly report by the 15th of the
    first month after the quarter ends (Q2 2026 report → due 15 Jul 2026).
  * Reminder 1 goes to the founders on the 8th of that month (a week before).
  * Reminder 2 goes on the 15th (due date) if the report still isn't in.
  * Nothing is ever sent once the report is uploaded.

Status per company × quarter (shown in the tracker):
  uploaded              — quarterly report document exists for the period
  second_reminder_sent  — both reminders out, still no report
  first_reminder_sent   — first reminder out, still no report
  not_uploaded          — collection window open / nothing sent yet

`run_reminder_sweep()` is idempotent and safe to run daily (scheduler) or
manually from the tracker page.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "uploaded": "Uploaded",
    "second_reminder_sent": "Second reminder sent",
    "first_reminder_sent": "First reminder sent",
    "not_uploaded": "Not uploaded yet",
}

STATUS_BADGES = {
    "uploaded": "badge-success",
    "second_reminder_sent": "badge-error",
    "first_reminder_sent": "badge-warn",
    "not_uploaded": "badge-neutral",
}


# ---------------------------------------------------------------------------
# Period math
# ---------------------------------------------------------------------------

def due_date(year: int, quarter: int) -> date:
    """Reports are due the 15th of the first month after the quarter ends."""
    month = quarter * 3 + 1
    if month > 12:
        return date(year + 1, 1, 15)
    return date(year, month, 15)


def reminder1_date(year: int, quarter: int) -> date:
    d = due_date(year, quarter)
    return d.replace(day=8)


def current_collection_period(today: date | None = None) -> tuple[int, int]:
    """The quarter whose report is currently being collected = previous quarter."""
    today = today or date.today()
    q = (today.month - 1) // 3 + 1
    if q == 1:
        return today.year - 1, 4
    return today.year, q - 1


def recent_periods(n: int = 4, today: date | None = None) -> list[tuple[int, int]]:
    """Last n collection periods, most recent first."""
    year, q = current_collection_period(today)
    out = []
    for _ in range(n):
        out.append((year, q))
        q -= 1
        if q == 0:
            year, q = year - 1, 4
    return out


# ---------------------------------------------------------------------------
# Founder contacts
# ---------------------------------------------------------------------------

def get_founder_contacts(company, db: Session) -> list[dict]:
    """
    Founder contacts for a company: stored JSON if present, otherwise names
    prefilled from the Attio-synced venture (matched by name, emails empty).
    """
    if company.founder_contacts:
        try:
            data = json.loads(company.founder_contacts)
            if isinstance(data, list):
                return data
        except Exception:
            pass

    # Prefill names from the CRM venture with the same name
    try:
        from ..models import CrmVenture
        venture = db.scalar(
            select(CrmVenture).where(CrmVenture.name.ilike(company.name))
        )
        if venture and venture.founders:
            return [{"name": n.strip(), "email": ""}
                    for n in venture.founders.split(",") if n.strip()]
    except Exception as exc:
        log.debug("founder prefill failed for %s: %s", company.name, exc)
    return []


def founder_emails(company, db: Session) -> list[str]:
    return [c["email"].strip() for c in get_founder_contacts(company, db)
            if c.get("email", "").strip()]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _report_doc(db: Session, company_id: int, year: int, quarter: int):
    from ..models import Document, DocumentCategory
    return db.scalar(
        select(Document).where(
            Document.company_id == company_id,
            Document.document_category == DocumentCategory.quarterly_reporting,
            Document.reporting_year == year,
            Document.reporting_quarter == quarter,
        ).order_by(Document.created_at.desc())
    )


def _reminder_row(db: Session, company_id: int, year: int, quarter: int):
    from ..models import QuarterlyReminder
    return db.scalar(
        select(QuarterlyReminder).where(
            QuarterlyReminder.company_id == company_id,
            QuarterlyReminder.year == year,
            QuarterlyReminder.quarter == quarter,
        )
    )


def status_for(db: Session, company, year: int, quarter: int) -> dict:
    """Full tracker row state for one company × quarter."""
    doc = _report_doc(db, company.id, year, quarter)
    rem = _reminder_row(db, company.id, year, quarter)

    if doc:
        status = "uploaded"
    elif rem and rem.second_sent_at:
        status = "second_reminder_sent"
    elif rem and rem.first_sent_at:
        status = "first_reminder_sent"
    else:
        status = "not_uploaded"

    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "badge": STATUS_BADGES[status],
        "document": doc,
        "reminder": rem,
        "due": due_date(year, quarter),
        "overdue": status != "uploaded" and date.today() > due_date(year, quarter),
    }


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _email_body(company, year: int, quarter: int, which: int) -> tuple[str, str]:
    d = due_date(year, quarter)
    period = f"Q{quarter} {year}"
    if which == 1:
        subject = f"Reminder: {period} report for {company.name} due {d.strftime('%d %b %Y')}"
        body = (
            f"Hi,\n\n"
            f"A friendly reminder that the {period} quarterly report for "
            f"{company.name} is due on {d.strftime('%d %B %Y')}.\n\n"
            f"Please upload it to the shared Google Drive folder (or send it to us) "
            f"by the due date.\n\n"
            f"Thank you!\nMerantix Investor Relations"
        )
    else:
        subject = f"Due today: {period} report for {company.name}"
        body = (
            f"Hi,\n\n"
            f"The {period} quarterly report for {company.name} is due today "
            f"({d.strftime('%d %B %Y')}) and we haven't received it yet.\n\n"
            f"Please share it with us as soon as possible.\n\n"
            f"Thank you!\nMerantix Investor Relations"
        )
    return subject, body


def send_reminder(db: Session, company, year: int, quarter: int, which: int) -> dict:
    """Send reminder 1 or 2 for a company × quarter. Idempotent per reminder slot."""
    from ..models import QuarterlyReminder
    from .email_service import send_email

    state = status_for(db, company, year, quarter)
    if state["status"] == "uploaded":
        return {"ok": False, "message": f"{company.name}: report already uploaded."}

    rem = state["reminder"]
    if rem is None:
        rem = QuarterlyReminder(company_id=company.id, year=year, quarter=quarter)
        db.add(rem)
        db.flush()

    slot = "first_sent_at" if which == 1 else "second_sent_at"
    if getattr(rem, slot):
        return {"ok": False, "message": f"{company.name}: reminder {which} already sent."}

    emails = founder_emails(company, db)
    if not emails:
        rem.last_error = "No founder email on file."
        db.commit()
        return {"ok": False, "message": f"{company.name}: no founder email on file."}

    subject, body = _email_body(company, year, quarter, which)
    try:
        send_email(emails, subject, body)
    except RuntimeError as exc:
        rem.last_error = str(exc)
        db.commit()
        return {"ok": False, "message": f"{company.name}: {exc}"}

    setattr(rem, slot, datetime.utcnow())
    rem.last_error = None
    db.commit()
    return {"ok": True, "message": f"{company.name}: reminder {which} sent to {', '.join(emails)}."}


def run_reminder_sweep(db: Session, today: date | None = None) -> dict:
    """
    Daily sweep: for the quarter currently being collected, send reminder 1
    on/after the 8th and reminder 2 on/after the 15th to every company whose
    report is missing. Idempotent — each reminder goes out at most once.
    """
    from ..models import Company

    today = today or date.today()
    year, quarter = current_collection_period(today)
    r1, r2 = reminder1_date(year, quarter), due_date(year, quarter)

    sent1 = sent2 = skipped = failed = 0
    messages: list[str] = []

    if today < r1:
        return {"year": year, "quarter": quarter, "sent1": 0, "sent2": 0,
                "skipped": 0, "failed": 0,
                "message": f"Q{quarter} {year}: first reminders go out on {r1.strftime('%d %b')}."}

    companies = db.scalars(select(Company).order_by(Company.name)).all()
    for company in companies:
        state = status_for(db, company, year, quarter)
        if state["status"] == "uploaded":
            skipped += 1
            continue
        rem = state["reminder"]
        if today >= r1 and not (rem and rem.first_sent_at):
            result = send_reminder(db, company, year, quarter, which=1)
            (messages.append(result["message"]))
            sent1 += 1 if result["ok"] else 0
            failed += 0 if result["ok"] else 1
            continue  # never send both on the same day
        if today >= r2 and rem and rem.first_sent_at and not rem.second_sent_at:
            result = send_reminder(db, company, year, quarter, which=2)
            messages.append(result["message"])
            sent2 += 1 if result["ok"] else 0
            failed += 0 if result["ok"] else 1

    summary = (f"Q{quarter} {year} sweep: {sent1} first + {sent2} second reminders sent, "
               f"{skipped} already uploaded, {failed} failed/skipped.")
    log.info("reminder sweep: %s %s", summary, messages)
    return {"year": year, "quarter": quarter, "sent1": sent1, "sent2": sent2,
            "skipped": skipped, "failed": failed, "message": summary}


# ---------------------------------------------------------------------------
# Attio auto-fill
# ---------------------------------------------------------------------------

def refresh_founders_from_attio(company, db: Session) -> dict:
    """
    Auto-fill founder contacts (names + emails) from Attio.

    Matches the company to its Attio-synced venture by name, fetches the
    People linked to that Attio company record, and keeps the ones whose
    name matches the venture's founders list (so investors/other contacts
    linked to the company are not emailed). Saves to company.founder_contacts.
    """
    from ..models import CrmVenture
    from .attio_client import get_attio_api_key, query_people_for_company

    venture = db.scalar(select(CrmVenture).where(CrmVenture.name.ilike(company.name)))
    if not venture:
        return {"ok": False, "message": f"{company.name}: no matching Attio venture found."}
    if not venture.attio_record_id:
        return {"ok": False, "message": f"{company.name}: venture has no Attio record id."}

    api_key = get_attio_api_key(db)
    if not api_key:
        return {"ok": False, "message": "Attio API key is not configured."}

    people = query_people_for_company(venture.attio_record_id, api_key)
    if not people:
        return {"ok": False, "message": f"{company.name}: no people found on the Attio record."}

    founder_names = [n.strip().lower() for n in (venture.founders or "").split(",") if n.strip()]

    def _is_founder(person_name: str) -> bool:
        if not founder_names:
            return True  # no founders list — take all linked people
        pn = person_name.lower()
        return any(fn in pn or pn in fn for fn in founder_names)

    contacts = [p for p in people if _is_founder(p["name"])]
    # Prefer entries with emails; dedupe by lowercased email/name
    seen: set[str] = set()
    cleaned: list[dict] = []
    for p in sorted(contacts, key=lambda x: (not x["email"],)):
        key = (p["email"] or p["name"]).lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(p)

    if not cleaned:
        return {"ok": False, "message": f"{company.name}: no founder matches among Attio people."}

    company.founder_contacts = json.dumps(cleaned, ensure_ascii=False)
    db.commit()
    with_email = sum(1 for p in cleaned if p["email"])
    return {"ok": True,
            "message": f"{company.name}: {len(cleaned)} founder(s) saved, {with_email} with email."}
