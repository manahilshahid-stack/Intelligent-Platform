"""Shared pytest fixtures.

The app reads DATABASE_URL and builds its engine at import time, so the test
environment is set here before anything from `app` is imported. Every test
run gets its own throwaway SQLite file; tables are emptied after each test.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ip-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ENABLE_SCHEDULER"] = "0"
for _name in ("OPENROUTER_API_KEY", "SMTP_USER", "SMTP_PASS", "ATTIO_API_KEY", "LUMA_API_KEY"):
    os.environ.pop(_name, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import LPUser, User, UserRole  # noqa: E402

PASSWORD = "Correct-Horse-9"


@pytest.fixture(scope="session", autouse=True)
def _app_started():
    # Entering the client runs the lifespan (migrations) once per session.
    with TestClient(app):
        yield


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def anon_client():
    return TestClient(app, follow_redirects=False)


def _login_client(email: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    resp = client.post("/login", data={"email": email, "password": PASSWORD})
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def admin_client(db):
    db.add(User(name="Admin", email="admin@example.test",
                password_hash=hash_password(PASSWORD), role=UserRole.admin))
    db.commit()
    return _login_client("admin@example.test")


@pytest.fixture
def user_client(db):
    db.add(User(name="Member", email="member@example.test",
                password_hash=hash_password(PASSWORD), role=UserRole.user))
    db.commit()
    return _login_client("member@example.test")


@pytest.fixture
def make_lp_user(db):
    """Create an LP user directly in the DB (OTP email cannot be sent in tests)."""
    def _make(email: str = "lp@example.test", *, verified: bool = True,
              otp_code: str | None = None) -> LPUser:
        user = LPUser(
            name="Test LP",
            email=email,
            password_hash=hash_password(PASSWORD),
            email_verified=verified,
            otp_code=otp_code,
            otp_expires_at=(datetime.utcnow() + timedelta(minutes=15)) if otp_code else None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    return _make


@pytest.fixture
def lp_token(anon_client, make_lp_user):
    make_lp_user()
    resp = anon_client.post("/api/lp/login", json={"email": "lp@example.test", "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]
