"""
History-aware query rewriting ("condense question") — Phase 1.5.

The retriever only ever sees the words of the *current* question, so a follow-up
like "tell me about their team structure" has no anchor and fetches the wrong
company. This step rewrites such a follow-up into a standalone search query
("Vara team structure") using the recent conversation, so retrieval pulls the
right documents. Used by BOTH the internal chat and the LP chat.

Reuses the configured OpenRouter chat model. It only fires when the question is
actually context-dependent (short, or contains a referential word), and it falls
back to the original message on any problem — so it can never break a chat.
"""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_REQUEST_TIMEOUT = 30
_MAX_TURNS = 6          # how many recent messages to consider
_TURN_CHARS = 400       # truncate each turn in the prompt
_MAX_QUERY_CHARS = 300  # sanity cap on the rewritten query

# Referential cues that signal the question depends on earlier context.
_REFERENTIAL = re.compile(
    r"\b(it|its|it's|their|theirs|them|they|that|this|these|those|"
    r"he|she|his|her|him|same|also|there|above|previous|former|latter)\b",
    re.IGNORECASE,
)

_SYSTEM = (
    "You help a document-retrieval system understand a follow-up message in a "
    "conversation. Output ONLY a JSON object with exactly two keys:\n"
    '  "query":   a single standalone search query for the latest message, with all '
    "pronouns and references (it, they, their, that company, the above, …) resolved "
    "to explicit names using the conversation. If the message is already "
    "self-contained, copy it.\n"
    '  "company": the ONE specific company the latest message is about, copied exactly '
    "as it is named in the conversation. Use null if there is no single company, or if "
    "it is ambiguous or refers to multiple companies.\n"
    "Output the JSON and nothing else."
)


def _needs_rewrite(message: str, prior_messages: list) -> bool:
    """Only invoke the LLM when there's history AND the message looks context-dependent."""
    if not prior_messages:
        return False
    words = message.split()
    return len(words) <= 12 or bool(_REFERENTIAL.search(message))


def _parse_json(text: str) -> dict:
    import json
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
        return {}


def condense_query(message: str, previous_messages: list, db) -> tuple[str, str | None]:
    """
    Resolve a follow-up against the recent conversation.

    Returns (search_query, focus_company):
      • search_query  — standalone query with references resolved (or the original).
      • focus_company — the single company the question is about, or None.

    Falls back to (message, None) when rewriting isn't needed or anything fails, so
    it can never break a chat.
    """
    prior = list(previous_messages or [])
    if not _needs_rewrite(message, prior):
        return message, None

    from ..config import settings as _cfg
    from .settings_service import get_openrouter_api_key

    api_key = get_openrouter_api_key(db)
    if not api_key:
        return message, None

    recent = prior[-_MAX_TURNS:]
    convo = "\n".join(
        f"{getattr(m, 'role', 'user')}: {(getattr(m, 'content', '') or '')[:_TURN_CHARS]}"
        for m in recent
    )
    user_msg = f"Conversation:\n{convo}\n\nLatest message: {message}\n\nJSON:"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://portfolio-intelligence.app",
        "X-Title": "Portfolio Intelligence Platform",
    }
    payload = {
        "model": _cfg.openrouter_chat_model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
    }

    try:
        resp = httpx.post(_OPENROUTER_CHAT_URL, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            log.warning("condense_query: OpenRouter HTTP %s — using original query", resp.status_code)
            return message, None
        content = resp.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    except Exception as exc:
        log.warning("condense_query: failed (%s) — using original query", exc)
        return message, None

    data = _parse_json(content)
    query = (data.get("query") or message) if isinstance(data, dict) else message
    query = str(query).strip().strip('"').strip()
    if not query or len(query) > _MAX_QUERY_CHARS:
        query = message

    company = data.get("company") if isinstance(data, dict) else None
    if isinstance(company, str):
        company = company.strip().strip('"').strip()
        if not company or company.lower() in ("null", "none", "n/a", "unknown"):
            company = None
    else:
        company = None

    log.info("condense_query: %r -> query=%r company=%r", message[:50], query[:60], company)
    return query, company
