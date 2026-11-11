# Regression: ISSUE-001 — /api/lp/verify-otp issued a token for any verified LP without code or password
# Found by /qa on 2026-11-11
# Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-11-11.md


def test_verify_otp_does_not_issue_token_for_already_verified_account(anon_client, make_lp_user):
    make_lp_user("victim@example.test", verified=True)

    resp = anon_client.post("/api/lp/verify-otp", json={"email": "victim@example.test", "code": "000000"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "already_verified"
    assert "token" not in resp.json()


def test_verify_otp_still_issues_token_for_correct_code(anon_client, make_lp_user, db):
    from app.models import LPUser

    make_lp_user("new@example.test", verified=False, otp_code="123456")

    resp = anon_client.post("/api/lp/verify-otp", json={"email": "new@example.test", "code": "123456"})

    assert resp.status_code == 200
    assert resp.json()["token"]
    db.expire_all()
    assert db.query(LPUser).filter_by(email="new@example.test").one().email_verified is True
