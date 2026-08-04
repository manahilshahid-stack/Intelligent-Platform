"""
Luma calendar sync — feeds the "Upcoming events" card on the LP home page.

Uses the official Luma Public API (https://docs.lu.ma). The API key is created
per-calendar (Luma → calendar settings → API, Plus feature), so it already
identifies the calendar: no calendar ID is needed.

Env vars (Railway):
  LUMA_API_KEY        required — per-calendar key
  LUMA_CALENDAR_URL   optional — plain public URL, used for "View all" link

Runs nightly via the scheduler (LUMA_SYNC_CRON, default 06:15 UTC) and can be
triggered from the admin News page. Upserts by luma_id, so it is idempotent.
Events are never deleted here; past events simply stop being served by the
LP endpoint (which filters starts_at >= now).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PortalEvent

log = logging.getLogger(__name__)

_LIST_EVENTS_URL = "https://api.lu.ma/public/v1/calendar/list-events"
_TIMEOUT = 30.0


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # store naive UTC, consistent with the rest of the schema
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _location_of(event: dict) -> str | None:
    geo = event.get("geo_address_json") or {}
    if isinstance(geo, dict):
        parts = [geo.get("address"), geo.get("city_state")]
        loc = ", ".join(p for p in parts if p)
        if loc:
            return loc[:500]
    if event.get("location_type") == "online" or event.get("virtual_info"):
        return "Online"
    return None


def sync_luma_events(db: Session) -> dict:
    """Pull events from the Luma calendar and upsert them. Returns a summary."""
    api_key = settings.luma_api_key
    if not api_key:
        return {"ok": False, "message": "LUMA_API_KEY is not configured on Railway."}

    headers = {"accept": "application/json", "x-luma-api-key": api_key}
    # Recent past too, so "past highlights" stay linkable; LP endpoint filters.
    after = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()

    added = updated = 0
    cursor: str | None = None
    pages = 0
    try:
        while True:
            params: dict = {"pagination_limit": 50, "after": after}
            if cursor:
                params["pagination_cursor"] = cursor
            resp = httpx.get(_LIST_EVENTS_URL, headers=headers, params=params, timeout=_TIMEOUT)
            if resp.status_code == 401:
                return {"ok": False, "message": "Luma rejected the API key (HTTP 401). Re-create it in calendar settings → API."}
            if resp.status_code != 200:
                return {"ok": False, "message": f"Luma API returned HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()

            for entry in data.get("entries", []):
                event = entry.get("event") or entry  # tolerate both shapes
                luma_id = event.get("api_id") or entry.get("api_id")
                name = (event.get("name") or "").strip()
                if not luma_id or not name:
                    continue
                row = db.scalar(select(PortalEvent).where(PortalEvent.luma_id == luma_id))
                if row is None:
                    row = PortalEvent(luma_id=luma_id, name=name[:500])
                    db.add(row)
                    added += 1
                else:
                    updated += 1
                row.name = name[:500]
                row.url = (event.get("url") or "")[:1000] or row.url
                row.cover_url = (event.get("cover_url") or "")[:1000] or row.cover_url
                row.location = _location_of(event) or row.location
                row.starts_at = _parse_dt(event.get("start_at")) or row.starts_at
                row.ends_at = _parse_dt(event.get("end_at")) or row.ends_at

            pages += 1
            if not data.get("has_more") or not data.get("next_cursor") or pages >= 20:
                break
            cursor = data["next_cursor"]

        db.commit()
    except httpx.HTTPError as exc:
        db.rollback()
        return {"ok": False, "message": f"Luma request failed: {exc}"}

    msg = f"Luma sync complete: {added} new, {updated} updated."
    log.info(msg)
    return {"ok": True, "message": msg, "added": added, "updated": updated}
