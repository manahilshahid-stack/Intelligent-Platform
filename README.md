# Portfolio Intelligence Platform

A FastAPI-based portfolio intelligence tool for venture capital teams.  
Upload portfolio company documents → extract KPIs with an LLM → review and approve → chat with your portfolio data.

---

## Features

- **Document upload** — PDF, DOCX, XLSX, XLSM, PPTX, TXT
- **Automatic text extraction** — pdfplumber, openpyxl, python-docx, python-pptx
- **LLM KPI extraction** — OpenRouter (GPT-4o-mini by default)
- **Admin review workflow** — structured field editing before approval
- **Embedding + retrieval** — cosine similarity over approved chunks
- **Chat** — RAG-based Q&A grounded in approved portfolio data
- **Role-based access control** — admin vs user, company scoping

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | `sqlite:///./dev.db` | PostgreSQL URL from Railway (or SQLite for local dev) |
| `SECRET_KEY` | Yes | *(ephemeral — sessions reset on restart)* | Random string used to sign session tokens |
| `OPENROUTER_API_KEY` | No | *(set via Admin → Settings)* | OpenRouter API key — takes precedence over DB setting |
| `OPENROUTER_CHAT_MODEL` | No | `openai/gpt-4o-mini` | Model used for KPI extraction and chat |
| `OPENROUTER_EMBEDDING_MODEL` | No | `openai/text-embedding-3-small` | Model used for chunk embedding |
| `MAX_UPLOAD_MB` | No | `20` | Maximum upload file size in MB |
| `SESSION_TTL_HOURS` | No | `72` | Session cookie lifetime in hours |
| `COOKIE_SECURE` | No | *(auto-set on Railway)* | Set to `1` to force `Secure` flag on cookies |

Railway automatically sets `RAILWAY_ENVIRONMENT`, which enables secure cookies. You do not need to set `COOKIE_SECURE` manually on Railway.

---

## Local Development

### Prerequisites

- Python 3.12+
- (Optional) PostgreSQL — SQLite is used by default

### Setup

```bash
git clone https://github.com/oliverschmied02-ai/intelligence-platform
cd intelligence-platform

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: copy and edit environment variables
cp .env.example .env   # or set them in your shell

uvicorn app.main:app --reload
```

The app starts at [http://localhost:8000](http://localhost:8000).  
Migrations run automatically on startup.

### Environment for local dev

Minimum `.env` for local development:

```env
SECRET_KEY=any-long-random-string-for-local-dev
# DATABASE_URL is optional — defaults to SQLite
# OPENROUTER_API_KEY=sk-or-...
```

---

## Create the First Admin User

The first registered user is always created with role `user`.  
Promote them to admin via the database directly:

**SQLite (local dev):**
```bash
sqlite3 dev.db "UPDATE users SET role='admin' WHERE email='your@email.com';"
```

**Railway Postgres (production):**
```bash
# Open a Railway shell for your service
railway run python3 -c "
from app.database import SessionLocal
from app.models import User, UserRole
db = SessionLocal()
u = db.query(User).filter_by(email='your@email.com').first()
u.role = UserRole.admin
db.commit()
print('Done')
"
```

After that, all user role management can be done via **Admin → Users**.

---

## Railway Deployment

### 1. Create a new project

1. Go to [railway.app](https://railway.app) and create a new project.
2. Add a **PostgreSQL** service to the project.
3. Add a new service from your GitHub repository.

### 2. Set environment variables

In the Railway service's **Variables** tab, add:

```
DATABASE_URL     = ${{Postgres.DATABASE_URL}}   # auto-linked from Postgres service
SECRET_KEY       = <generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
OPENROUTER_API_KEY = sk-or-...                  # optional — can be set via Admin UI instead
```

### 3. Deploy

Railway detects `railway.toml` and uses the `Dockerfile` automatically.  
The build installs all dependencies; the container runs:

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Migrations run on every startup via `run_migrations()` — safe to run repeatedly.

### 4. First login

1. Visit your Railway deployment URL.
2. Register an account.
3. Promote yourself to admin (see above).
4. Log in as admin.

---

## Configure OpenRouter

OpenRouter provides access to GPT-4o-mini and text-embedding-3-small.

1. Sign up at [openrouter.ai](https://openrouter.ai).
2. Create an API key.
3. Either:
   - Set `OPENROUTER_API_KEY` as a Railway environment variable, **or**
   - Go to **Admin → Settings** in the app and paste the key there.

The environment variable takes precedence over the database setting.

---

## Testing the Full Workflow

### 1. Upload and extract a document

1. Log in as **admin**.
2. Go to **Admin → Companies** — create a company.
3. Go to **Admin → Users** — assign yourself (or a test user) to the company.
4. Go to **Documents → Upload** — upload a PDF or DOCX portfolio update.
5. The app extracts text automatically. If an OpenRouter key is configured, LLM extraction runs immediately.
6. Open the document detail page to see the lifecycle status.

### 2. Review and approve

1. Go to **Admin → Review queue** (or click the badge on the Admin home).
2. Open the extraction for your document.
3. Review the extracted fields — edit any that are incorrect.
4. Click **Approve & embed**. This saves `corrected_json`, marks the document approved, and creates embedding chunks in Postgres.

### 3. Chat

1. Go to **Chat**.
2. Ask a question about the approved document, e.g.:
   - *"What is the cash position of Acme AI?"*
   - *"Which companies have less than 12 months runway?"*
   - *"Summarise the key risks across all portfolio companies."*
3. The answer cites the source chunks used (`[#1]`, `[#2]`, etc.).

### 4. Verify access control

- Log in as a **regular user** assigned to Company A.
- Confirm you cannot access `/admin` (403).
- Confirm you can only see Company A's documents.
- Confirm chat answers only reference Company A's approved data.

---

## Architecture

```
app/
  main.py               FastAPI app + lifespan migrations
  config.py             Settings from env vars
  models.py             SQLAlchemy ORM models
  migrations.py         Idempotent schema migrations (create_all + ALTER TABLE)
  auth.py               Session management, password hashing, access guards
  templates.py          Jinja2 setup + custom filters
  routes/
    auth_routes.py      /register /login /logout /dashboard
    admin_routes.py     /admin /admin/companies /admin/users
    settings_routes.py  /admin/settings
    document_routes.py  /documents /documents/upload /documents/{id}
    review_routes.py    /admin/review /admin/review/{id} approve/reject
    chat_routes.py      /chat
  services/
    extraction_service.py    Text extraction (PDF/DOCX/XLSX/PPTX/TXT)
    portfolio_extraction.py  LLM KPI extraction via OpenRouter
    embeddings.py            extraction_to_text, chunk_text, embed_text, embed_approved_extraction
    retrieval.py             retrieve_relevant_chunks (cosine similarity in Python)
    chat_service.py          build_context, call_chat
    settings_service.py      OpenRouter key storage (env var > DB)
  prompts/
    portfolio_extraction.py  System + user prompt for KPI extraction
  templates/            Jinja2 HTML templates
```

**Database**: Railway Postgres in production, SQLite for local dev.  
**Embeddings**: stored as JSON-serialised `list[float]` in `chunks.embedding` (TEXT column). Cosine similarity computed in Python — no pgvector required.  
**File storage**: raw bytes stored temporarily in Postgres during upload; cleared after text extraction. Text is kept in `documents.raw_text`. No S3 required.
