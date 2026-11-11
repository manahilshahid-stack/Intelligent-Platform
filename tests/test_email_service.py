"""Unit tests for app.services.email_service (Resend over HTTPS). No real network calls."""
from __future__ import annotations

import httpx
import pytest

from app.services import email_service


class _Recorder:
    """Stand-in for httpx.post that records the call and returns a canned response."""

    def __init__(self, status: int = 200, json_body: dict | None = None, raises: Exception | None = None):
        self.status, self.json_body, self.raises = status, json_body or {"id": "email_123"}, raises
        self.calls: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json=self.json_body, request=httpx.Request("POST", url))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "Merantix Capital <lp@example.test>")


@pytest.fixture
def fake_post(monkeypatch):
    def _install(**kwargs) -> _Recorder:
        rec = _Recorder(**kwargs)
        monkeypatch.setattr(email_service.httpx, "post", rec)
        return rec
    return _install


def test_email_configured_true_when_key_and_sender_set(configured):
    assert email_service.email_configured() is True


@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "EMAIL_FROM"])
def test_email_configured_false_when_either_var_missing(configured, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert email_service.email_configured() is False


def test_send_email_posts_html_to_resend_with_timeout(configured, fake_post):
    rec = fake_post()

    email_service.send_email(["lp@example.test"], "Your code", html="<p>123456</p>")

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"] == {"Authorization": "Bearer re_test_key"}
    assert call["timeout"] == email_service.TIMEOUT_SECONDS
    assert call["json"] == {
        "from": "Merantix Capital <lp@example.test>",
        "to": ["lp@example.test"],
        "subject": "Your code",
        "html": "<p>123456</p>",
    }


def test_send_email_sends_plain_text_body(configured, fake_post):
    rec = fake_post()

    email_service.send_email(["a@example.test", "b@example.test"], "Reminder", "Please upload.")

    payload = rec.calls[0]["json"]
    assert payload["text"] == "Please upload."
    assert "html" not in payload
    assert payload["to"] == ["a@example.test", "b@example.test"]


def test_send_email_raises_without_network_call_when_not_configured(fake_post):
    rec = fake_post()

    with pytest.raises(RuntimeError, match="RESEND_API_KEY and EMAIL_FROM"):
        email_service.send_email(["lp@example.test"], "s", "b")
    assert rec.calls == []


def test_send_email_raises_without_recipients(configured, fake_post):
    rec = fake_post()

    with pytest.raises(RuntimeError, match="No recipient"):
        email_service.send_email([], "s", "b")
    assert rec.calls == []


def test_send_email_raises_without_body(configured, fake_post):
    rec = fake_post()

    with pytest.raises(RuntimeError, match="no body"):
        email_service.send_email(["lp@example.test"], "s")
    assert rec.calls == []


def test_send_email_raises_readable_error_on_resend_rejection(configured, fake_post):
    fake_post(status=403, json_body={"message": "The domain is not verified."})

    with pytest.raises(RuntimeError) as exc:
        email_service.send_email(["lp@example.test"], "s", "b")

    assert "403" in str(exc.value)
    assert "domain is not verified" in str(exc.value)
    assert "re_test_key" not in str(exc.value)


def test_send_email_raises_on_non_json_error_body(configured, monkeypatch):
    def _post(url, **kwargs):
        return httpx.Response(502, text="Bad Gateway", request=httpx.Request("POST", url))
    monkeypatch.setattr(email_service.httpx, "post", _post)

    with pytest.raises(RuntimeError, match="502: Bad Gateway"):
        email_service.send_email(["lp@example.test"], "s", "b")


def test_send_email_raises_instead_of_hanging_on_timeout(configured, fake_post):
    fake_post(raises=httpx.ReadTimeout("timed out"))

    with pytest.raises(RuntimeError, match="ReadTimeout"):
        email_service.send_email(["lp@example.test"], "s", "b")
