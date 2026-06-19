from __future__ import annotations

import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


class Settings:
    # ── Database ──────────────────────────────────────────────────────────────
    # Railway provides DATABASE_URL as postgres://... which database.py rewrites
    # to postgresql+psycopg://...  SQLite is used for local dev when not set.
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")

    # ── Session security ──────────────────────────────────────────────────────
    # Must be set to a stable random string in production via Railway env var.
    # If not set, a new random key is generated each restart (invalidates sessions).
    secret_key: str = os.environ.get("SECRET_KEY", "")

    # ── OpenRouter ────────────────────────────────────────────────────────────
    # Can be set here or via Admin → Settings in the UI (stored in app_settings).
    openrouter_api_key: str | None = os.environ.get("OPENROUTER_API_KEY")
    openrouter_chat_model: str = os.environ.get(
        "OPENROUTER_CHAT_MODEL", "openai/gpt-4o-mini"
    )
    openrouter_embedding_model: str = os.environ.get(
        "OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small"
    )

    # ── Upload limits ─────────────────────────────────────────────────────────
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024

    # ── Session TTL ───────────────────────────────────────────────────────────
    session_ttl_hours: int = int(os.environ.get("SESSION_TTL_HOURS", "72"))

    def __post_init__(self) -> None:
        pass

    def __init__(self) -> None:
        if not self.secret_key:
            generated = secrets.token_hex(32)
            self.secret_key = generated
            log.warning(
                "SECRET_KEY is not set — generated an ephemeral key for this process. "
                "Sessions will be invalidated on restart. "
                "Set SECRET_KEY as a Railway environment variable."
            )


settings = Settings()