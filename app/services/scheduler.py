"""
Background scheduler — automatic, hands-off re-indexing.

Runs the full pipeline (pull from Attio + Google Drive, then re-embed) on a
cron schedule so nobody has to click "reindex" manually. Thanks to the
skip-unchanged check in the indexer, recurring runs only embed new or edited
notes/files, so a daily run is cheap.

Config (Railway env vars, all optional):
  ENABLE_SCHEDULER       "1" (default) to run; "0" to disable.
  REINDEX_CRON           crontab string. Default "0 3 * * *" (daily 03:00).
  SYNC_INTERVAL_MINUTES  if set, ALSO run a full refresh every N minutes
                         (e.g. "15" for every 15 min). Useful when webhooks
                         are not configured. Set to "0" or leave unset to
                         rely on REINDEX_CRON only.
  SCHEDULER_TZ           timezone for the cron. Default "UTC".
  REINDEX_ON_STARTUP     "1" to also run once ~1 min after boot. Default off.

Notes
-----
* Runs in-process (APScheduler BackgroundScheduler) — no extra Railway service.
* The app starts ONE uvicorn worker, so there is exactly one scheduler. If you
  ever scale to multiple workers/replicas, gate this to a single instance to
  avoid duplicate runs.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_scheduler = None  # module-level singleton


def _truthy(val: str | None) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# The job: full refresh
# ---------------------------------------------------------------------------

def run_full_refresh() -> None:
    """
    Pull everything from the sources and re-embed it. Mirrors the manual admin
    buttons but runs end-to-end. Each step is isolated so one failure does not
    abort the rest. Uses its own DB session (safe from a scheduler thread).
    """
    from ..database import SessionLocal

    db = SessionLocal()
    log.info("scheduled refresh: START")
    try:
        for name, fn in _STEPS:
            try:
                log.info("scheduled refresh: %s …", name)
                fn(db)
            except Exception as exc:  # noqa: BLE001 — never let one step kill the run
                log.error("scheduled refresh: step '%s' failed: %s", name, exc, exc_info=True)
    finally:
        db.close()
        log.info("scheduled refresh: END")


def run_reminder_sweep_job() -> None:
    """Daily quarterly-report reminder sweep (own DB session, never raises)."""
    from ..database import SessionLocal
    from .reminder_service import run_reminder_sweep

    db = SessionLocal()
    try:
        result = run_reminder_sweep(db)
        log.info("reminder sweep: %s", result.get("message"))
    except Exception as exc:  # noqa: BLE001
        log.error("reminder sweep failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _sync_ventures(db):
    from .attio_sync import sync_attio_list_ventures
    sync_attio_list_ventures(db)


def _index_ventures(db):
    from .knowledge_indexer import index_all_crm_ventures
    index_all_crm_ventures(db)


def _sync_notes(db):
    from .attio_sync import sync_attio_notes_for_ventures
    sync_attio_notes_for_ventures(db)


def _index_notes(db):
    from .knowledge_indexer import index_all_crm_notes
    index_all_crm_notes(db)


def _sync_files(db):
    from .attio_sync import sync_attio_files_for_ventures
    sync_attio_files_for_ventures(db)


def _index_files(db):
    from .knowledge_indexer import index_all_crm_files
    index_all_crm_files(db)


def _ingest_gdrive(db):
    from .gdrive_ingest import ingest_external_documents
    ingest_external_documents(db)


def _poll_drive_changes(db):
    from .gdrive_ingest import poll_drive_changes
    poll_drive_changes(db)


_STEPS = [
    ("sync ventures (Attio list)", _sync_ventures),
    ("index ventures", _index_ventures),
    ("sync notes (Attio)", _sync_notes),
    ("index notes", _index_notes),
    ("sync files (Attio)", _sync_files),
    ("index files", _index_files),
    ("ingest Google Drive docs (linked)", _ingest_gdrive),
    ("poll Google Drive changes (direct uploads)", _poll_drive_changes),
]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_scheduler():
    """Start the background scheduler if enabled. Idempotent."""
    global _scheduler

    if not _truthy(os.getenv("ENABLE_SCHEDULER", "1")):
        log.info("Scheduler disabled (ENABLE_SCHEDULER is not truthy).")
        return None
    if _scheduler is not None:
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.date import DateTrigger
    except Exception as exc:  # noqa: BLE001
        log.warning("APScheduler unavailable — automatic re-index disabled: %s", exc)
        return None

    cron = os.getenv("REINDEX_CRON", "0 3 * * *")  # daily at 03:00
    tz = os.getenv("SCHEDULER_TZ", "UTC")

    sched = BackgroundScheduler(timezone=tz)
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
    except Exception as exc:  # noqa: BLE001
        log.error("Invalid REINDEX_CRON %r (%s) — falling back to daily 03:00.", cron, exc)
        trigger = CronTrigger.from_crontab("0 3 * * *", timezone=tz)

    sched.add_job(
        run_full_refresh,
        trigger=trigger,
        id="full_refresh",
        max_instances=1,       # never overlap with a still-running refresh
        coalesce=True,         # collapse missed runs into a single run
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # Daily quarterly-report reminder sweep (default 09:00). Sends reminder 1
    # on the 8th and reminder 2 on the 15th of the month after each quarter,
    # to founders of companies whose report is missing. Idempotent.
    reminder_cron = os.getenv("REMINDER_CRON", "0 9 * * *")
    try:
        reminder_trigger = CronTrigger.from_crontab(reminder_cron, timezone=tz)
    except Exception:
        reminder_trigger = CronTrigger.from_crontab("0 9 * * *", timezone=tz)
    sched.add_job(
        run_reminder_sweep_job,
        trigger=reminder_trigger,
        id="reminder_sweep",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # Optional: interval-based polling (runs every N minutes in addition to cron)
    interval_minutes = int(os.getenv("SYNC_INTERVAL_MINUTES", "0") or "0")
    if interval_minutes > 0:
        from apscheduler.triggers.interval import IntervalTrigger
        sched.add_job(
            run_full_refresh,
            trigger=IntervalTrigger(minutes=interval_minutes, timezone=tz),
            id="interval_refresh",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        log.info("Scheduler: interval refresh every %d minutes enabled.", interval_minutes)

    if _truthy(os.getenv("REINDEX_ON_STARTUP", "0")):
        from datetime import datetime, timedelta, timezone as _tz
        sched.add_job(
            run_full_refresh,
            trigger=DateTrigger(run_date=datetime.now(_tz.utc) + timedelta(minutes=1)),
            id="startup_refresh",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        log.info("Scheduler: a one-off refresh will run ~1 minute after startup.")

    sched.start()
    _scheduler = sched
    log.info("Scheduler started: automatic full refresh on cron %r (%s).", cron, tz)
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None
        log.info("Scheduler stopped.")
