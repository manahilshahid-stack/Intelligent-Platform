# Database Schema — Merantix LP Intelligence Platform

> Railway PostgreSQL · Last updated: July 2026

---

## Plain English Overview

The database is split into **4 functional areas**. Think of them as separate rooms that occasionally talk to each other.

```
┌─────────────────────┐   ┌─────────────────────┐
│   LP PORTAL         │   │   ATTIO / CRM DATA  │
│   (what LPs see)    │   │   (powers the AI)   │
└─────────────────────┘   └─────────────────────┘
┌─────────────────────┐   ┌─────────────────────┐
│   KNOWLEDGE BASE    │   │   ADMIN PORTAL      │
│   (AI's memory)     │   │   (internal team)   │
└─────────────────────┘   └─────────────────────┘
```

---

## Area 1 — LP Portal Tables

These tables exist purely for LPs (Limited Partners) who log in to the portal.

### `lp_users`
Stores each LP's account.
```sql
CREATE TABLE lp_users (
  id                   integer NOT NULL,         -- unique identifier
  email                varchar(254) NOT NULL,    -- login email, must be unique
  password_hash        varchar(256) NOT NULL,    -- bcrypt hashed password
  name                 varchar(200),             -- display name
  organization         varchar(200),             -- their fund/firm
  interest_areas       text,                     -- JSON array e.g. ["Healthtech","AI"]
  looking_for          text,                     -- JSON array of onboarding selections
  about_yourself       text,                     -- free text bio
  onboarding_completed boolean NOT NULL,         -- false until onboarding is done
  created_at           timestamp NOT NULL,
  updated_at           timestamp NOT NULL
);
```
**Why:** When an LP registers, a row is created here. `interest_areas` and `looking_for` are stored as JSON arrays because they're multi-select lists — simpler than a join table for this use case.

---

### `lp_user_sessions`
Tracks who is logged in. Uses server-side sessions (not JWTs).
```sql
CREATE TABLE lp_user_sessions (
  id          integer NOT NULL,
  lp_user_id  integer NOT NULL,          -- FK → lp_users.id
  token_hash  varchar(64) NOT NULL,      -- SHA-256 hash of the bearer token
  expires_at  timestamp NOT NULL,        -- session TTL (default 72h)
  created_at  timestamp NOT NULL
);
```
**Why:** When an LP logs in, a random token is generated, hashed, and stored here. The raw token goes to the browser. On each request, the token is hashed and looked up in this table. Expired rows are cleaned up automatically.

---

### `lp_chat_sessions`
Each conversation an LP starts is a session.
```sql
CREATE TABLE lp_chat_sessions (
  id          integer NOT NULL,
  lp_user_id  integer NOT NULL,    -- FK → lp_users.id (CASCADE delete)
  created_at  timestamp NOT NULL
);
```
**Why:** Thin table — just a container for messages. One LP can have many sessions (one per conversation).

---

### `lp_chat_messages`
Every message in every LP conversation.
```sql
CREATE TABLE lp_chat_messages (
  id             integer NOT NULL,
  session_id     integer NOT NULL,    -- FK → lp_chat_sessions.id (CASCADE delete)
  role           varchar(20) NOT NULL, -- "user" or "assistant"
  content        text NOT NULL,        -- the message text
  citations_json text,                 -- JSON array of source references used
  created_at     timestamp NOT NULL
);
```
**Why:** `citations_json` stores which knowledge chunks the AI used to generate the answer — this is what powers the source citations shown in responses.

---

## Area 2 — Attio / CRM Data Tables

These tables mirror what's in Attio. They're populated via webhook + scheduled sync.

### `crm_ventures`
Every company Merantix has ever looked at — portfolio, pipeline, passed, everything.
```sql
CREATE TABLE crm_ventures (
  id               integer NOT NULL,
  attio_entry_id   varchar(128),       -- Attio list entry ID (primary dedup key)
  attio_list_id    varchar(128),       -- which Attio list it came from
  attio_record_id  varchar(128),       -- Attio company record ID
  name             varchar(500),
  website          varchar(500),
  description      text,
  stage            varchar(200),       -- e.g. "Active Portfolio", "Passed", "DD"
  sector           varchar(200),
  owner            varchar(200),       -- Merantix team member assigned
  source           varchar(200),
  status           varchar(200),
  attio_url        varchar(500),       -- link back to Attio record
  raw_entry_json   text,               -- full Attio JSON (deferred, only loaded when needed)
  raw_record_json  text,               -- full Attio record JSON (deferred)
  raw_attio_json   text,               -- backup raw blob (deferred)
  synced_at        timestamp NOT NULL,
  created_at       timestamp NOT NULL,
  updated_at       timestamp NOT NULL
);
-- 3,221 rows total · 21 are portfolio-stage
```
**Why:** `stage` is the critical field — it's what determines whether a company appears in the LP portal ("Active Portfolio") or not. The three raw JSON columns store the full Attio payloads so re-indexing can happen without hitting the Attio API again (though we nulled these out to save space — they get repopulated on next sync).

---

### `crm_notes`
Notes written by the Merantix team on companies in Attio.
```sql
CREATE TABLE crm_notes (
  id               integer NOT NULL,
  attio_note_id    varchar(128) NOT NULL,   -- unique Attio note ID
  crm_venture_id   integer,                  -- FK → crm_ventures.id (SET NULL on delete)
  attio_record_id  varchar(128),
  title            varchar(512),
  content_text     text,                     -- full note content (deferred)
  raw_note_json    text,                     -- Attio raw JSON (deferred)
  created_by       varchar(256),             -- who wrote it in Attio
  created_at_attio timestamp,
  synced_at        timestamp NOT NULL,
  created_at       timestamp NOT NULL,
  updated_at       timestamp NOT NULL
);
-- 295 rows · each becomes a knowledge chunk
```
**Why:** Notes are the richest source of qualitative data about companies. `created_by` is stored but redacted before LPs see it — the name of who wrote the note is internal.

---

### `crm_files`
Files attached to Attio company records.
```sql
CREATE TABLE crm_files (
  id                 integer NOT NULL,
  attio_file_id      varchar(256) NOT NULL,
  crm_venture_id     integer,              -- FK → crm_ventures.id
  filename           varchar(512),
  file_type          varchar(64),
  mime_type          varchar(128),
  sha256             varchar(64),          -- dedup: don't re-download unchanged files
  raw_text           text,                 -- extracted text content
  extraction_status  USER-DEFINED NOT NULL, -- pending|extracted|failed
  synced_at          timestamp,
  created_at         timestamp NOT NULL,
  updated_at         timestamp NOT NULL
);
```
**Why:** `sha256` is used to detect if a file has changed — if the hash matches what we have, we skip re-downloading. `raw_text` is the extracted text that gets chunked into `knowledge_chunks`.

---

### `external_documents`
Google Drive links found inside Attio records — auto-fetched and indexed.
```sql
CREATE TABLE external_documents (
  id             integer NOT NULL,
  url            varchar(1000) NOT NULL,   -- unique Drive URL
  provider       varchar(32) NOT NULL,     -- "gdrive"
  kind           varchar(32),              -- doc|sheet|slides|drive_file
  crm_venture_id integer,                  -- FK → crm_ventures.id
  title          varchar(512),
  sha256         varchar(64),              -- change detection
  raw_text       text,                     -- extracted content (deferred)
  status         USER-DEFINED NOT NULL,    -- pending|fetched|no_access|failed
  fetched_at     timestamp,
  created_at     timestamp NOT NULL,
  updated_at     timestamp NOT NULL
);
-- 70 documents → become gdrive-type knowledge chunks
```

---

### `crm_sync_runs`
Audit log of every sync that ran.
```sql
CREATE TABLE crm_sync_runs (
  id              integer NOT NULL,
  sync_type       varchar(50) NOT NULL,   -- attio_list|notes_sync|files_sync etc.
  status          USER-DEFINED NOT NULL,  -- running|completed|failed
  started_at      timestamp NOT NULL,
  finished_at     timestamp,
  records_total   integer,
  records_seen    integer NOT NULL,
  records_created integer NOT NULL,
  records_updated integer NOT NULL,
  error           text                    -- error message if failed
);
```
**Why:** Pure audit trail — tells you when syncs ran, how many records were processed, and whether anything failed. The admin UI shows this as a progress page during manual syncs.

---

## Area 3 — Knowledge Base Tables (RAG Memory)

These tables are what the AI actually searches when answering questions.

### `knowledge_sources`
One row per indexable unit (a company record, a note, a file).
```sql
CREATE TABLE knowledge_sources (
  id             integer NOT NULL,
  source_type    varchar(50) NOT NULL,   -- crm_venture|crm_note|crm_file|gdrive
  source_id      integer NOT NULL,       -- ID in the originating table
  crm_venture_id integer,               -- FK → crm_ventures.id (CASCADE delete)
  title          varchar(512),
  visibility     varchar(20) NOT NULL,   -- "admin" or "all"
  approved       boolean NOT NULL,       -- must be true to be searchable
  created_at     timestamp NOT NULL,
  updated_at     timestamp NOT NULL
);
```
**Why:** Acts as a registry. Deleting a source cascades and removes all its chunks — clean cleanup when a company is removed.

---

### `knowledge_chunks` ⭐ Most important table
The actual searchable text with vector embeddings. Every AI answer comes from here.
```sql
CREATE TABLE knowledge_chunks (
  id                  integer NOT NULL,
  knowledge_source_id integer NOT NULL,    -- FK → knowledge_sources.id (CASCADE)
  crm_venture_id      integer,             -- FK → crm_ventures.id (CASCADE)
  source_type         varchar(50) NOT NULL, -- crm_venture|crm_note|gdrive
  source_id           integer NOT NULL,
  text                text NOT NULL,        -- the chunk content
  sanitized_text      text,                 -- LP-safe version (financials stripped)
  embedding           text,                 -- legacy JSON "[0.12, -0.34, ...]"
  embedding_vec       vector(1536),         -- native pgvector type ✓ HNSW indexed
  sector              varchar(256),
  themes_json         text,                 -- JSON array of inferred themes
  visibility          varchar(20) NOT NULL,
  approved            boolean NOT NULL,
  created_at          timestamp NOT NULL
);
-- 3,586 rows · all have embedding_vec populated · HNSW index active
```
**Why `embedding` AND `embedding_vec`:** The `embedding` text column is the legacy format (JSON string). `embedding_vec` is the native pgvector type — 4x smaller, 10x faster for similarity search. The HNSW index on `embedding_vec` means finding the top-25 similar chunks is a near-instant indexed lookup rather than scanning all 3,586 rows.

**Why `sanitized_text`:** When a note contains internal details (financials, names), the full text is in `text` (admin only) and the cleaned version is in `sanitized_text` (served to LPs). The code applies regex redaction to strip confidential lines.

---

## Area 4 — Admin Portal Tables

Used by the internal Merantix team, not exposed to LPs.

### `users`
Admin accounts for the internal team.
```sql
CREATE TABLE users (
  id            integer NOT NULL,
  email         varchar(254) NOT NULL,
  password_hash varchar(256) NOT NULL,
  name          varchar(200),
  role          USER-DEFINED NOT NULL,   -- admin|user
  company_id    integer,                  -- FK → companies.id
  created_at    timestamp NOT NULL
);
```

### `companies`
Portfolio companies for the purpose of document management (separate from `crm_ventures`).
```sql
CREATE TABLE companies (
  id          integer NOT NULL,
  name        varchar(200) NOT NULL,
  slug        varchar(80) NOT NULL,   -- URL-safe identifier, unique
  description text,
  created_at  timestamp NOT NULL
);
```

### `documents`
PDFs, pitch decks, reports uploaded manually via the admin interface.
```sql
CREATE TABLE documents (
  id                   integer NOT NULL,
  company_id           integer NOT NULL,
  uploaded_by_id       integer NOT NULL,
  title                varchar(512) NOT NULL,
  filename             varchar(256) NOT NULL,
  file_type            varchar(16) NOT NULL,
  sha256               varchar(64),           -- dedup
  file_bytes           bytea,                 -- raw file (nulled after extraction)
  raw_text             text,
  upload_status        USER-DEFINED NOT NULL,  -- uploaded|processing|done|failed
  extraction_status    USER-DEFINED NOT NULL,
  review_status        USER-DEFINED NOT NULL,  -- pending|approved|rejected
  document_category    USER-DEFINED NOT NULL,
  is_regular_reporting boolean NOT NULL,
  reporting_period     varchar(20),            -- e.g. "Q1 2025"
  created_at           timestamp NOT NULL
);
```

### `chunks`
Text chunks from admin-uploaded documents (separate from `knowledge_chunks`).
```sql
CREATE TABLE chunks (
  id            integer NOT NULL,
  document_id   integer NOT NULL,
  company_id    integer NOT NULL,
  chunk_type    USER-DEFINED NOT NULL,
  text          text NOT NULL,
  embedding     text,               -- legacy JSON
  embedding_vec vector(1536),       -- native pgvector ✓
  approved      boolean NOT NULL,   -- must be approved to appear in chat
  created_at    timestamp NOT NULL
);
```
**Why separate from `knowledge_chunks`:** Admin-uploaded docs go through a review workflow (upload → extract → approve) before becoming searchable. `knowledge_chunks` is for auto-synced Attio data which is trusted by default.

### `app_settings`
Key-value config store — no hardcoded secrets.
```sql
CREATE TABLE app_settings (
  id         integer NOT NULL,
  key        varchar(128) NOT NULL,   -- e.g. "openrouter_api_key"
  value      text,                    -- the setting value
  created_at timestamp NOT NULL,
  updated_at timestamp NOT NULL
);
```

---

## Table Sizes (current)

| Table | Rows | Size | Notes |
|---|---|---|---|
| `knowledge_chunks` | 3,586 | 150 MB | Largest — embeddings dominate |
| `crm_ventures` | 3,221 | 15 MB | After nulling raw JSON blobs |
| `crm_notes` | 295 | 13 MB | Note content |
| `lp_chat_messages` | — | 592 KB | Grows with usage |
| `external_documents` | 70 | 368 KB | Google Drive docs |

---

## Key Design Decisions

1. **Two separate user systems** (`users` vs `lp_users`) — different trust levels, different auth flows, completely isolated.

2. **Attio data in raw JSON columns** (`raw_entry_json` etc.) — store the full Attio payload so re-indexing doesn't require hitting the Attio API again. These are `deferred` so they don't load on list queries.

3. **`knowledge_chunks` has two embedding columns** — `embedding` (legacy text) and `embedding_vec` (native pgvector). Both are maintained during migration. The HNSW index on `embedding_vec` makes similarity search O(log n) instead of O(n).

4. **`sanitized_text` on chunks** — allows serving different content to admins vs LPs from the same table without duplicating rows.

5. **`approved` flag on chunks** — nothing becomes searchable until explicitly approved. Default is `true` for auto-synced CRM data, `false` for manually uploaded documents (require review).

6. **SHA-256 on files/documents** — used for deduplication. If a file hasn't changed (same hash), skip re-downloading and re-indexing.
