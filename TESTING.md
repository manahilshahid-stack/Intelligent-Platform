# Testing

**Framework:** pytest 8 + pytest-cov, using FastAPI's `TestClient` against the real app.

## Run

```bash
pip install -r requirements-dev.txt
pytest                                   # whole suite
pytest --cov=app --cov-report=term-missing
pytest tests/test_smoke.py -k healthz    # one test
```

CI runs the same command on every push and pull request (`.github/workflows/test.yml`).

## How the suite is wired

- `tests/conftest.py` sets `DATABASE_URL` to a throwaway SQLite file, a fixed
  `SECRET_KEY` and `ENABLE_SCHEDULER=0` **before** importing `app`, and unsets
  third-party keys (OpenRouter, SMTP, Attio, Luma) so no test talks to the network.
- The app lifespan (migrations) runs once per session; every table is emptied
  after each test.
- Fixtures: `anon_client`, `user_client` (logged-in non-admin), `admin_client`,
  `db` (SQLAlchemy session), `make_lp_user(...)`, `lp_token` (bearer token for a
  verified LP user).

## Layers

- **Unit:** pure helpers (parsers, categorisers) — call the function directly.
- **Integration (default):** HTTP request through `TestClient`, then assert the
  response *and* the resulting DB state.
- **Smoke:** `tests/test_smoke.py` — health, auth redirects, admin gate, LP login.
- **E2E/browser:** not automated; use `/qa` for browser passes.

## Conventions

- File per area: `tests/test_<area>.py`; regression tests found by `/qa` live in
  `tests/test_<component>_regression_N.py` (Python module names cannot contain
  dots or hyphens) and start with a `# Regression: ISSUE-NNN` comment.
- Test names state the behaviour: `test_<thing>_<expected outcome>`.
- Assert on status code **and** the effect (body field, DB row, absence of a row).
- Never call real external services; leave their env vars unset or mock the client.

Aim for 100% coverage of new code: every new function gets a test, every bug fix
gets a regression test, and error paths get tested alongside the happy path.
