"""
News feed — the fund's own Substack publication mirrored onto the LP home page.

Per Manahil (Aug 2026): ALL portal news comes from the Merantix Substack and
nowhere else. No Google News, no external press scraping, no LLM filtering.
You publish on Substack → the portal picks it up (hourly by default) and sorts
each post into one of three containers:

  funding    — post announces a fundraise / investment / round
  portfolio  — post is about a specific portfolio company
  merantix   — everything else (fund news, newsletters, event recaps)

Categorisation is a transparent keyword + portfolio-name heuristic (post tags
on Substack win if present). Posts are first-party content, so they publish
automatically (status='approved'); the admin News page can still Hide or 📌
Pin any post. Items from older non-Substack sources are hidden automatically.

Env vars:
  SUBSTACK_URL        required — e.g. https://merantix.substack.com
  SUBSTACK_MAX_POSTS  optional — posts per fetch (default 20)
  NEWS_FETCH_CRON     optional — schedule (default hourly, "0 * * * *")
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CrmVenture, NewsItem

log = logging.getLogger(__name__)

_TIMEOUT = 20.0
_SUBSTACK_URL = os.environ.get("SUBSTACK_URL", "").strip().rstrip("/")
_SUBSTACK_MAX = int(os.environ.get("SUBSTACK_MAX_POSTS", "20"))
# Posts older than this never enter the feed (default 6 months).
_MAX_AGE_DAYS = int(os.environ.get("NEWS_MAX_AGE_DAYS", "183"))

_FUNDING_RE = re.compile(
    r"\b(rais(?:e|es|ed|ing)|funding|series [a-e]\b|seed round|pre-seed"
    r"|investment round|closes? (?:a |its )?round|led by|oversubscribed"
    r"|new fund|fund [iv]+\b|capital raise)\b",
    re.IGNORECASE,
)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _portfolio_names(db: Session) -> list[str]:
    names = db.scalars(
        select(CrmVenture.name).where(CrmVenture.stage.ilike("portfolio"))
    ).all()
    return sorted({n.strip() for n in names if n and n.strip()})


def _categorize(title: str, description: str, tags: list[str], portfolio_names: list[str]) -> str:
    """funding | portfolio | merantix — Substack post tags win, then keywords."""
    tagset = " ".join(tags).lower()
    if "funding" in tagset:
        return "funding"
    if "portfolio" in tagset:
        return "portfolio"
    if "merantix" in tagset:
        return "merantix"

    text = f"{title} {description}".lower()
    if _FUNDING_RE.search(text):
        return "funding"
    if any(name.lower() in text for name in portfolio_names):
        return "portfolio"
    return "merantix"


def _fetch_substack() -> list[dict]:
    """Latest posts from the publication's public RSS feed (no key needed)."""
    if not _SUBSTACK_URL:
        return []
    feed_url = _SUBSTACK_URL + "/feed"
    try:
        resp = httpx.get(feed_url, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (httpx.HTTPError, ET.ParseError) as exc:
        log.warning("news: Substack feed fetch failed (%s): %s", feed_url, exc)
        return []

    publication = (root.findtext("channel/title") or "Substack").strip()
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        enclosure = item.find("enclosure")
        image = (enclosure.get("url") or "").strip() if enclosure is not None else ""
        items.append({
            "title": title[:700],
            "url": link[:1000],
            "source": publication[:300],
            "image_url": image[:1000] or None,
            "description": _strip_html(item.findtext("description") or "")[:2000],
            "tags": [(c.text or "").strip() for c in item.findall("category")],
            "published_at": _parse_pubdate(item.findtext("pubDate")),
        })
        if len(items) >= _SUBSTACK_MAX:
            break
    return items


def fetch_news(db: Session) -> dict:
    """Mirror the Substack feed into news_items. Idempotent; returns a summary."""
    if not _SUBSTACK_URL:
        return {"ok": False,
                "message": "SUBSTACK_URL is not configured on Railway — the news feed is fed from Substack only."}

    posts = _fetch_substack()
    if not posts:
        return {"ok": False,
                "message": f"Substack feed returned no posts (checked {_SUBSTACK_URL}/feed). "
                           "Is the publication public?"}

    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=_MAX_AGE_DAYS)
    names = _portfolio_names(db)
    added = 0
    for item in posts:
        if item["published_at"] and item["published_at"] < cutoff:
            continue  # older than the age window (default 6 months)
        h = _url_hash(item["url"])
        if db.scalar(select(NewsItem.id).where(NewsItem.url_hash == h)):
            continue
        category = _categorize(item["title"], item["description"], item["tags"], names)
        db.add(NewsItem(
            url=item["url"], url_hash=h, title=item["title"],
            source=item["source"], company="Merantix Capital",
            category=category, status="approved",
            image_url=item["image_url"], published_at=item["published_at"],
        ))
        added += 1

    # The feed is Substack-only: hide anything visible from other sources
    # (e.g. items left over from the earlier Google News pipeline).
    domain = _SUBSTACK_URL.split("//", 1)[-1]
    cleaned = 0
    for n in db.scalars(
        select(NewsItem).where(NewsItem.status.in_(("pending", "approved")))
    ).all():
        if domain not in n.url:
            n.status = "hidden"
            n.pinned = False
            cleaned += 1

    db.commit()
    msg = f"Substack sync complete: {added} new post(s) published."
    if cleaned:
        msg += f" {cleaned} non-Substack item(s) removed from the feed."
    log.info(msg)
    return {"ok": True, "message": msg, "added": added, "cleaned": cleaned}
