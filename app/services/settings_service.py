"""
Application settings service.

Reads and writes named settings from the app_settings table.
Encryption can be added here later without touching call sites.
"""
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting

OPENROUTER_API_KEY = "openrouter_api_key"
ATTIO_API_KEY = "attio_api_key"
ATTIO_OBJECT_SLUG = "attio_object_slug"
ATTIO_LIST_ID_OR_SLUG = "attio_list_id_or_slug"


# ---------------------------------------------------------------------------
# Low-level get / set (plain text for MVP — swap bodies here to add encryption)
# ---------------------------------------------------------------------------

def _get(key: str, db: Session) -> str | None:
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    return row.value if row else None


def _set(key: str, value: str, db: Session) -> None:
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def _delete(key: str, db: Session) -> None:
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    if row:
        db.delete(row)
        db.commit()


# ---------------------------------------------------------------------------
# OpenRouter API key
# ---------------------------------------------------------------------------

def get_openrouter_api_key(db: Session) -> str | None:
    """
    Return the active OpenRouter API key.
    Environment variable takes precedence over the database value.
    """
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key
    return _get(OPENROUTER_API_KEY, db)


def set_openrouter_api_key(value: str, db: Session) -> None:
    value = value.strip()
    if not value:
        raise ValueError("API key must not be empty.")
    _set(OPENROUTER_API_KEY, value, db)


def has_env_openrouter_key() -> bool:
    """True when the key comes from the environment (read-only from UI perspective)."""
    return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())


# ---------------------------------------------------------------------------
# Attio settings
# ---------------------------------------------------------------------------

def get_attio_api_key(db: Session) -> str | None:
    env_key = os.environ.get("ATTIO_API_KEY", "").strip()
    if env_key:
        return env_key
    return _get(ATTIO_API_KEY, db)


def set_attio_api_key(value: str, db: Session) -> None:
    value = value.strip()
    if not value:
        raise ValueError("API key must not be empty.")
    _set(ATTIO_API_KEY, value, db)


def get_attio_object_slug(db: Session) -> str:
    return _get(ATTIO_OBJECT_SLUG, db) or "companies"


def set_attio_object_slug(value: str, db: Session) -> None:
    _set(ATTIO_OBJECT_SLUG, value.strip() or "companies", db)


def get_attio_list_id_or_slug(db: Session) -> str | None:
    return _get(ATTIO_LIST_ID_OR_SLUG, db) or None


def set_attio_list_id_or_slug(value: str | None, db: Session) -> None:
    v = (value or "").strip()
    if v:
        _set(ATTIO_LIST_ID_OR_SLUG, v, db)
    else:
        _delete(ATTIO_LIST_ID_OR_SLUG, db)


def has_env_attio_key() -> bool:
    return bool(os.environ.get("ATTIO_API_KEY", "").strip())


def mask_key(key: str | None) -> str | None:
    """Return a masked version for display: sk-or-...XXXX (last 4 chars visible)."""
    if not key:
        return None
    visible = key[-4:] if len(key) >= 4 else key
    return f"{'*' * min(24, max(8, len(key) - 4))}...{visible}"
