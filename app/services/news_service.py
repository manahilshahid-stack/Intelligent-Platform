"""
News fetcher — feeds the (admin-curated) news feed on the LP home page.

Pipeline, nightly (NEWS_FETCH_CRON, default 06:30 UTC):
  1. Query Google News RSS (free, keyless) for "Merantix Capital" and each
     portfolio company name (crm_ventures.stage = Portfolio).
  2. Drop items older than NEWS_MAX_AGE_DAYS (default 30) and dedupe by URL.
  3. ONE cheap-model call per fetch classifies the new headlines:
     wrong-company matches are discarded, the rest labelled funding | press.
     Items from the Merantix Capital query are labelled merantix.
  4. Everything lands as status='pending'. NOTHING reaches LPs until an admin
     approves it on /admin/news. `pinned` items feed the highlight banner.

Idempotent: re-running never duplicates (url_hash unique) and never touches
items an admin has already approved/hidden.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CrmVenture, NewsItem

log = logging.getLogger(__name__)

_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
_TIMEOUT = 20.0
_MAX_PER_QUERY = 8
_MAX_AGE_DAYS = int(os.environ.get("NEWS_MAX_AGE_DAYS", "30"))

# Own Substack publication (e.g. https://merantix.substack.com). Its posts are
# trusted first-party content: they bypass the review queue (auto-approved,
# Merantix tab). Admin can still hide individual posts.
_SUBSTACK_URL = os.environ.get("SUBSTACK_URL", "").strip().rstrip("/")
_SUBSTACK_MAX = int(os.environ.get("SUBSTACK_MAX_POSTS", "5"))


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _fetch_rss(query: str) -> list[dict]:
    """Fetch one Google News RSS query → [{title, url, source, published_at}]."""
    url = _RSS_URL.format(query=quote(f'"{query}"'))
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except (httpx.HTTPError, ET.ParseError) as exc:
        log.warning("news: RSS fetch failed for %r: %s", query, exc)
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        published = _parse_pubdate(item.findtext("pubDate"))
        if not title or not link:
            continue
        # Google News titles end with " - Publication"; keep both parts.
        if not source and " - " in title:
            source = title.rsplit(" - ", 1)[-1].strip()
        items.append({"title": title[:700], "url": link[:1000],
                      "source": source[:300] or None, "published_at": published})
        if len(items) >= _MAX_PER_QUERY:
            break
    return items


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)


def _fetch_og_image(article_url: str) -> str | None:
    """Best-effort thumbnail: fetch the article page and read its og:image tag."""
    try:
        resp = httpx.get(article_url, timeout=10.0, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; MerantixPortal/1.0)"})
        if resp.status_code != 200:
            return None
        m = _OG_IMAGE_RE.search(resp.text[:60_000])
        if m:
            img = (m.group(1) or m.group(2) or "").strip()
            if img.startswith("http"):
                return img[:1000]
    except httpx.HTTPError:
        pass
    return None


def _fetch_substack() -> list[dict]:
    """Latest posts from the fund's own Substack (public RSS, no key needed)."""
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
            "published_at": _parse_pubdate(item.findtext("pubDate")),
        })
        if len(items) >= _SUBSTACK_MAX:
            break
    return items


def _portfolio_names(db: Session) -> list[str]:
    names = db.scalars(
        select(CrmVenture.name).where(CrmVenture.stage.ilike("portfolio"))
    ).all()
    return sorted({n.strip() for n in names if n and n.strip()})


def _classify(candidates: list[dict], db: Session) -> dict[int, dict]:
    """
    One pipeline-model call: {index: {"keep": bool, "category": "funding"|"press"}}.
    On any failure returns {} — callers then default to keep=True, category=press
    (admin approval is the real gate, so a failed classification is not fatal).
    """
    if not candidates:
        return {}
    try:
        from ..config import settings as _settings
        from .portfolio_extraction import _call_openrouter, parse_llm_json
        from .settings_service import get_openrouter_api_key
        api_key = get_openrouter_api_key(db)
        if not api_key:
            return {}
        listing = [{"i": i, "company": c["company"], "headline": c["title"]}
                   for i, c in enumerate(candidates)]
        prompt = (
            "You are filtering news headlines for a venture fund's investor page.\n"
            "For EACH item, decide:\n"
            '  keep: false if the headline is clearly about a DIFFERENT entity that '
            "merely shares the company's name, or is spam/irrelevant. Otherwise true.\n"
            '  category: "funding" if it announces a fundraise/investment/acquisition '
            'involving the company, else "press".\n\n'
            f"Items:\n{json.dumps(listing, ensure_ascii=False)}\n\n"
            'Respond with ONLY a JSON object mapping index to result, e.g. '
            '{"0": {"keep": true, "category": "funding"}, "1": {"keep": false, "category": "press"}}.'
        )
        raw = _call_openrouter([{"role": "user", "content": prompt}],
                               api_key=api_key, model=_settings.openrouter_pipeline_model)
        mapping = parse_llm_json(raw) or {}
        out: dict[int, dict] = {}
        for k, v in mapping.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, dict):
                out[idx] = {"keep": bool(v.get("keep", True)),
                            "category": "funding" if v.get("category") == "funding" else "press"}
        return out
    except Exception as exc:  # noqa: BLE001 — classification is best-effort
        log.warning("news: classification failed (%s) — keeping items unclassified", exc)
        return {}


def fetch_news(db: Session) -> dict:
    """Full fetch cycle. Returns a summary dict."""
    queries = [("Merantix Capital", "merantix")] + [
        (name, None) for name in _portfolio_names(db)
    ]

    cutoff = datetime.utcnow() - timedelta(days=_MAX_AGE_DAYS)
    candidates: list[dict] = []
    seen_hashes: set[str] = set()

    # Own Substack posts first: trusted, auto-approved, no age cutoff
    # (newsletters publish less often than news breaks).
    substack_added = 0
    for item in _fetch_substack():
        h = _url_hash(item["url"])
        if h in seen_hashes or db.scalar(select(NewsItem.id).where(NewsItem.url_hash == h)):
            continue
        seen_hashes.add(h)
        db.add(NewsItem(
            url=item["url"], url_hash=h, title=item["title"],
            source=item["source"], company="Merantix Capital",
            category="merantix", status="approved",
            image_url=item.get("image_url"),
            published_at=item["published_at"],
        ))
        substack_added += 1

    for query, forced_category in queries:
        for item in _fetch_rss(query):
            # STRICT relevance: the headline itself must name the company.
            # Anything vaguer never even reaches the review queue.
            if query.lower() not in item["title"].lower():
                continue
            if item["published_at"] and item["published_at"] < cutoff:
                continue
            h = _url_hash(item["url"])
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            if db.scalar(select(NewsItem.id).where(NewsItem.url_hash == h)):
                continue  # already stored on a previous run
            candidates.append({**item, "url_hash": h, "company": query,
                               "forced_category": forced_category})

    if not candidates:
        db.commit()
        suffix = f" {substack_added} Substack post(s) published." if substack_added else ""
        return {"ok": True, "message": f"News fetch complete: nothing new from the press.{suffix}",
                "added": substack_added}

    verdicts = _classify(
        [c for c in candidates if c["forced_category"] is None], db
    )
    # verdict indices refer to the classify sublist — rebuild alignment:
    classify_list = [c for c in candidates if c["forced_category"] is None]

    added = dropped = 0
    for c in candidates:
        status = "pending"
        if c["forced_category"]:
            category = c["forced_category"]
        else:
            v = verdicts.get(classify_list.index(c), {"keep": True, "category": "press"})
            category = v["category"]
            if not v["keep"]:
                # store as hidden (not skipped) so nightly runs never re-fetch
                # and re-classify the same wrong-company/irrelevant headline
                status = "hidden"
                dropped += 1
        db.add(NewsItem(
            url=c["url"], url_hash=c["url_hash"], title=c["title"],
            source=c["source"], company=c["company"], category=category,
            published_at=c["published_at"], status=status,
            # thumbnail only for items that will actually be reviewed/shown
            image_url=_fetch_og_image(c["url"]) if status == "pending" else None,
        ))
        if status == "pending":
            added += 1

    # Backfill thumbnails for existing visible items that predate image support
    for n in db.scalars(
        select(NewsItem).where(NewsItem.status.in_(("pending", "approved")),
                               NewsItem.image_url.is_(None)).limit(20)
    ).all():
        n.image_url = _fetch_og_image(n.url)

    db.commit()
    msg = f"News fetch complete: {added} new item(s) awaiting review, {dropped} filtered out."
    if substack_added:
        msg += f" {substack_added} Substack post(s) published directly."
    log.info(msg)
    return {"ok": True, "message": msg, "added": added, "dropped": dropped,
            "substack": substack_added}
