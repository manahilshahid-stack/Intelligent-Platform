# Regression: LP-019 — OTP emails could not be sent on Railway Hobby (outbound SMTP blocked),
# and the SMTP call had no timeout, so register/resend-otp requests hung instead of failing.
# Fix: all email goes through Resend's HTTPS API with a timeout (app/services/email_service.py).
from __future__ import annotations

import httpx
import pytest

from app.models import LPUser
from app.services import email_service


@pytest.fixture
def sent(monkeypatch):
    """Configure Resend and capture every email the app sends."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "Merantix Capital <lp@example.test>")
    calls: list[dict] = []

    def _post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"id": "email_123"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(email_service.httpx, "post", _post)
    return calls


def test_register_emails_the_stored_otp_via_resend(anon_client, db, sent):
    resp = anon_client.post("/api/lp/register", json={
        "first_name": "Ada", "last_name": "Lovelace", "email": "Ada@Example.test", "password": "long-enough-pw",
    })

    assert resp.status_code == 200
    user = db.query(LPUser).filter_by(email="ada@example.test").one()
    assert len(sent) == 1
    assert sent[0]["to"] == ["ada@example.test"]
    assert sent[0]["subject"] == "Your Merantix LP Portal verification code"
    assert user.otp_code in sent[0]["html"]
    assert "Hi Ada," in sent[0]["html"]


def test_resend_otp_emails_a_fresh_code(anon_client, db, make_lp_user, sent):
    make_lp_user("pending@example.test", verified=False, otp_code="111111")

    resp = anon_client.post("/api/lp/resend-otp", json={"email": "pending@example.test"})

    assert resp.status_code == 200
    db.expire_all()
    new_code = db.query(LPUser).filter_by(email="pending@example.test").one().otp_code
    assert new_code and new_code != "111111"
    assert len(sent) == 1 and new_code in sent[0]["html"]


def test_resend_otp_returns_503_instead_of_hanging_when_email_times_out(anon_client, make_lp_user, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "Merantix Capital <lp@example.test>")

    def _timeout(url, **kwargs):
        raise httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(email_service.httpx, "post", _timeout)
    make_lp_user("pending@example.test", verified=False, otp_code="111111")

    resp = anon_client.post("/api/lp/resend-otp", json={"email": "pending@example.test"})

    assert resp.status_code == 503


def test_resend_otp_sends_nothing_for_verified_account(anon_client, make_lp_user, sent):
    make_lp_user("done@example.test", verified=True)

    resp = anon_client.post("/api/lp/resend-otp", json={"email": "done@example.test"})

    assert resp.status_code == 200
    assert sent == []


def test_reporting_tracker_warns_when_email_not_configured(admin_client):
    resp = admin_client.get("/admin/reporting-tracker")

    assert resp.status_code == 200
    assert "RESEND_API_KEY" in resp.text


def test_reporting_tracker_hides_warning_when_email_configured(admin_client, sent):
    resp = admin_client.get("/admin/reporting-tracker")

    assert resp.status_code == 200
    assert "RESEND_API_KEY" not in resp.text
