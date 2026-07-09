# Security Check — LP Intelligent Platform

Documentation of all data privacy and security measures for the Merantix LP-facing intelligence platform. Goal: **only sanitized, portfolio-scoped data is ever accessible to LPs.**

Last updated: 2026-07-09

---

## 1. Data Sanitization for LPs (implemented)

| Measure | Where | What it does |
|---|---|---|
| Sensitivity tagging at ingestion | `ingestion/pipeline.py` — `auto_tag_sensitivity()` | Every chunk is auto-classified (`live_commercials`, `pass_rationale`, `founder_personal`, `third_party_confidential`, `thesis`) and assigned an `audience_mask` |
| Sensitivity stored in schema | `ingestion/db_schema.sql` | `sensitivity_class` and `audience_mask` (JSONB) are mandatory fields on all chunks |
| Default-deny policy | `atlas-skill/SKILL.md` §5.2 | Chunks with unknown sensitivity are withheld from LP and Info audiences automatically |
| Portfolio-only hard filter | `PLATFORM_SKILL.md` | LP-facing retrieval is restricted to `crm_ventures.stage = 'Portfolio'`; pipeline and passed deals are invisible to LPs |
| Financial redaction | `PLATFORM_SKILL.md` — `redact_confidential()` | ARR, valuation, burn, stage details, and founder assessments are stripped from all LP-facing responses |
| Retrieval scope filtering | `PLATFORM_SKILL.md` — `viewer_scope="lp"` | Scope filter applied at query time to **both** ORM statements **and** raw pgvector SQL (`pg_extra`), closing the known filter-bypass bug pattern |
| LLM prompt guardrails | `PLATFORM_SKILL.md` — `_LP_GUARDRAIL` | System prompt rules prevent the agent from e.g. presenting pipeline companies as portfolio companies |

## 2. Platform & Infrastructure Security (implemented)

| Measure | Where | What it does |
|---|---|---|
| Webhook signature verification | `ingestion/attio_webhook.py` | HMAC-SHA256 verification of Attio webhooks with `hmac.compare_digest()` (constant-time comparison) |
| API authentication | `ingestion/attio_webhook.py` | Bearer token auth for Attio API calls via env var |
| Secrets management | all ingestion modules | `OPENAI_API_KEY`, `DATABASE_URL`, `ATTIO_WEBHOOK_SECRET`, Google credentials all loaded from environment variables — never committed to code |
| Least-privilege Drive access | `ingestion/drive_watcher.py` | Google service account restricted to `drive.readonly` scope |
| SQL injection protection | `ingestion/pipeline.py`, `ingestion/drive_watcher.py` | All queries parameterized (psycopg2 `%s` binding); no string-built SQL |
| Database network isolation | Railway | App connects via `postgres.railway.internal` (private network); public TCP proxy and HTTP domain removed |
| Content integrity | `ingestion/pipeline.py` | SHA-256 hash per chunk; re-embedding only on content change |
| Soft deletes / audit trail | `ingestion/db_schema.sql`, `ingestion/drive_watcher.py` | `deleted_at` timestamps instead of hard deletes; deletions from Drive are propagated as soft-deletes |
| Mandatory provenance | `ingestion/db_schema.sql` | Every chunk carries `source_id`, `source_type`, `company_slug`, `author`, `source_date` — no chunk without source + date |
| Input validation | `ingestion/drive_watcher.py` | MIME-type validation, binary files skipped, minimum-content checks |
| Structured logging | all ingestion modules | Ingest counts, polling operations, and errors logged for observability |

## 3. Planned — Agreed Design (not yet implemented)

### 3.1 User authentication (highest priority)
- **Method:** Supabase Auth with passwordless magic links (free tier, covers 1000+ users)
- **Access control:** `allowed_emails` allowlist table in Railway Postgres, imported from the compiled LP spreadsheet — only listed emails can receive a login link
- **Enforcement:** Railway backend verifies the JWT on every request, re-checks the email against `allowed_emails` (instant revocation by row deletion), and maps email → `viewer_scope` (`@merantix.com` → analyst; all others → lp)
- **Email delivery:** external SMTP (e.g. Resend free tier) for magic-link delivery at scale
- **Rule:** allowlist check lives in the backend only — never frontend-only

### 3.2 CORS configuration
- Added together with auth (JWT in `Authorization` header triggers browser preflight)
- Allowed origin locked to the Cloudflare frontend domain only; allowed headers: `Authorization`, `Content-Type`

## 4. Roadmap (from platform architecture)

- **Response validation layer** — verify LLM claims are grounded in retrieved chunks; scan responses for leaked confidential content before delivery
- **Master agent** — centralized access-control and redaction enforcement point
- **Red-team testing** — scheduled adversarial testing of the LP access boundary
- **Analyst viewer scope** — separate `viewer_scope="analyst"` route with full access

