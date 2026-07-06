"""
LLM-based listwise reranker (Phase 1.4).

"Free" / no-new-service: reuses the configured OpenRouter chat model rather than
a dedicated reranking API. Given the user question and the fused candidate
passages, it asks the model to order them by usefulness, then returns the top-k.

Robust by design: any failure (no API key, HTTP error, malformed output) falls
back to the input order, so reranking can never break retrieval. The interface
is intentionally simple so a dedicated reranker (Cohere/Voyage/Jina/local
cross-encoder) can be swapped in later behind `rerank_candidates`.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_REQUEST_TIMEOUT = 45
_MAX_CANDIDATES = 25      # cap passages sent to the reranker (bounds token cost)
_SNIPPET_CHARS = 500      # truncate each passage in the rerank prompt

_SYSTEM = (
    "You are a search reranker. You are given a user question and a numbered list "
    "of passages. Rank the passages from MOST to LEAST useful for answering the "
    "question. Return ONLY a comma-separated list of the passage numbers in ranked "
    "order (e.g. '3,1,5,2'). Include every number exactly once. Output nothing else."
)


def _parse_order(content: str, n: int) -> list[int]:
    """Parse the model's '3,1,5,...' output into 0-based indices within [0, n)."""
    seen: set[int] = set()
    order: list[int] = []
    for tok in re.findall(r"\d+", content or ""):
        idx = int(tok) - 1
        if 0 <= idx < n and idx not in seen:
            seen.add(idx)
            order.append(idx)
    return order


def rerank_candidates(query: str, candidates: list, db: "Session", top_k: int) -> list:
    """
    Reorder *candidates* (objects exposing `.text`) by relevance to *query* and
    return the top_k. Falls back to the input order (trimmed) on any problem.
    """
    if not candidates or len(candidates) <= 1:
        return candidates[:top_k]

    from ..config import settings as _cfg
    from .settings_service import get_openrouter_api_key

    api_key = get_openrouter_api_key(db)
    if not api_key:
        return candidates[:top_k]

    pool = candidates[:_MAX_CANDIDATES]
    listing = "\n".join(
        f"[{i + 1}] {(getattr(c, 'text', '') or '')[:_SNIPPET_CHARS]}"
        for i, c in enumerate(pool)
    )
    user_msg = f"Question: {query}\n\nPassages:\n{listing}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://portfolio-intelligence.app",
        "X-Title": "Portfolio Intelligence Platform",
    }
    # Use a fast cheap model for reranking — it only outputs a list of numbers,
    # not a quality response. This saves 5-10s vs using the main Sonnet model.
    rerank_model = os.environ.get("OPENROUTER_RERANK_MODEL", "openai/gpt-4o-mini")
    payload = {
        "model": rerank_model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }

    try:
        resp = httpx.post(_OPENROUTER_CHAT_URL, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            log.warning("rerank: OpenRouter HTTP %s — keeping fusion order", resp.status_code)
            return candidates[:top_k]
        content = resp.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    except Exception as exc:
        log.warning("rerank: failed (%s) — keeping fusion order", exc)
        return candidates[:top_k]

    order = _parse_order(content, len(pool))
    if not order:
        return candidates[:top_k]

    ranked = [pool[i] for i in order]
    # Append any pool items the model didn't mention, preserving original order.
    mentioned = set(order)
    ranked.extend(c for i, c in enumerate(pool) if i not in mentioned)
    # Plus any candidates beyond the rerank cap.
    ranked.extend(candidates[_MAX_CANDIDATES:])
    return ranked[:top_k]
