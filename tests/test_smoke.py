"""Baseline happy-path and access-control checks for the core app."""


def test_healthz(anon_client):
    resp = anon_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_anonymous_root_redirects_to_login(anon_client):
    resp = anon_client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_admin_pages_reject_non_admin(user_client):
    assert user_client.get("/admin").status_code == 403


def test_admin_can_open_admin_overview(admin_client):
    assert admin_client.get("/admin").status_code == 200


def test_lp_login_returns_token_for_verified_user(lp_token, anon_client):
    resp = anon_client.get("/api/lp/me", headers={"Authorization": f"Bearer {lp_token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "lp@example.test"
