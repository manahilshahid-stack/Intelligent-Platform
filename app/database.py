from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


def _build_url(raw: str) -> str:
    """Railway emits postgres:// — SQLAlchemy 2 requires postgresql+psycopg://."""
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg" + raw[len("postgres"):]
    elif raw.startswith("postgresql://") and "+psycopg" not in raw:
        raw = "postgresql+psycopg" + raw[len("postgresql"):]
    return raw


_url = _build_url(settings.database_url)
_is_sqlite = _url.startswith("sqlite")

engine = create_engine(
    _url,
    pool_pre_ping=not _is_sqlite,
    **({} if _is_sqlite else {"pool_size": 5, "max_overflow": 10}),
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
