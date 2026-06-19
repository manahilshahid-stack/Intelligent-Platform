# Portfolio Intelligence Platform — System Overview & Change Log

_A two-page reference for how the platform works and what has been built so far (Phases 0 → 1)._

---

## 1. What it does

A FastAPI app that lets a VC team and its stakeholders **ask questions and get evidence-grounded, cited answers** over three kinds of data: portfolio-company documents, the Attio CRM, and (soon) live web/trends. Content is embedded into a vector store and retrieved per question; an LLM composes the answer with inline citations. Access is strictly segregated by role.

## 2. Data sources & roles

**Three sources**
- **Portfolio documents** — quarterly/monthly updates, decks. Uploaded → text extracted → LLM extracts KPIs → **admin reviews & approves** → embedded.
- **Attio CRM** — company/venture records (structured fields), notes, and file attachments, synced and embedded.
- **Web / trends** — planned for Phase 3 (live scraping + search).

**Three roles (strict data segregation)**

| Role | Sees |
|---|---|
| **Admin** (VC team) | Everything, unredacted — all docs, CRM internals, financials, pipeline, notes/files. |
| **Company user** | Their **own** company's docs in full + **general** info on other companies (profile, sector, thesis/analysis). Financials, investment amounts, deal pipeline, and personal info on *other* companies are **stripped**. |
| **LP** | Read-only. Company/market info + investment signals, with financials and confidential internals **stripped** everywhere. No uploads, no document browsing. |

Approval gate: a company's own uploads are **not** queryable until an admin approves them.

## 3. The pipeline

**Ingestion:** upload/sync → extract text → (KPIs via LLM, admin-approved) → chunk → embed → store. CRM notes/files additionally get an **index-time sanitized copy** (LLM rewrite that removes money, deal terms, and names while keeping the substance) so non-admins can safely read them.

**Retrieval (per question):**
1. **Vector search** — semantic similarity (pgvector on Railway, Python cosine locally).
2. **Keyword search** — exact-term matching (catches names, metrics, dates).
3. **RRF fusion** — combine both ranked lists.
4. **Role gate** — swap notes/files for sanitized copies (drop if none); strip confidential fields per role.
5. **LLM rerank** — sharpen the fused pool down to the final top-k.
6. **Answer** — LLM composes a cited response; conversation history is included.

## 4. Technical diagram

```
 SOURCES                 INGESTION                         STORAGE
 ┌────────────┐   upload → extract text → LLM KPIs    ┌──────────────────────┐
 │ Portfolio  │ ───────→ admin approve → chunk → embed→│ chunks (+ vector)    │
 │ documents  │                                        │                      │
 ├────────────┤   sync → normalize → embed             │ knowledge_chunks     │
 │ Attio CRM  │ ───────→ (+ LLM SANITIZED copy for     │  (+ sanitized_text)  │
 │ notes/files│          notes & files)                │  (+ vector)          │
 ├────────────┤                                        └──────────┬───────────┘
 │ Web/trends │  (Phase 3 — not yet wired)                        │
 └────────────┘                                                   │
                                                                  ▼
 QUESTION ─► embed ─►  ┌─────────── RETRIEVE_FOR_CHAT ───────────────────────┐
                       │  vector search  +  keyword search                   │
                       │            └──── RRF fusion ────┘                    │
                       │                    │                                 │
                       │            ROLE GATE  (admin / company_user / lp):   │
                       │              • notes/files → sanitized copy or drop  │
                       │              • strip confidential fields             │
                       │                    │                                 │
                       │              LLM RERANK → top-k                      │
                       └────────────────────┬────────────────────────────────┘
                                            ▼
                       LLM answer  (+ inline [#n] citations, + chat history,
                                     + role guardrail prompt)  ─► user
```

**Dual-mode storage:** on **Railway (Postgres)** the `embedding_vec` column + HNSW index power fast pgvector search; on **localhost (SQLite)** the same code auto-falls back to Python cosine. Identical results, no extra local setup.

---

## 5. What has been built so far (change log)

### Phase 0 — Correctness fixes
- **Portfolio docs now reach chat.** Both chat routes previously queried only CRM knowledge; a new unified `retrieve_for_chat` merges portfolio documents + CRM.
- **Conversation memory restored** on the internal chat (it was dropping history).
- **Markdown rendering fixed** in the admin/internal chat (answers showed raw `###`/`**`; now rendered like the LP chat).

### Phase 0.5 — Strict role-based segregation
- Single **role-gated retrieval** with `viewer_scope` (admin / company_user / lp).
- **Confidential redaction** of structured fields (financials, funding/investment amounts, deal stage/probability, owner/contact) for non-admins.
- **Index-time LLM sanitization** of free-text CRM notes/files → stored in a new `sanitized_text` column; non-admins are served the sanitized copy, or the item is **dropped** (never raw) — fail-closed. Runs automatically on every sync; a one-time `backfill_sanitized_text()` covers pre-existing rows.
- **Defense-in-depth:** role-specific guardrail instructions added to the LLM prompt.

### Phase 1 — Retrieval quality (hybrid + reranked, dual-mode)
- **1.1 pgvector storage** — guarded, Postgres-only migration adds a `vector` column + HNSW index; additive and non-fatal (SQLite untouched, no new Python dependency).
- **1.2 pgvector retrieval + write-population** — DB-side ANN query with automatic Python fallback; new embeddings populate the vector column on write so fresh data is immediately searchable. (Also fixed a latent `_embed` bug that would have crashed file indexing.)
- **1.3 hybrid search** — keyword retrieval (dialect-safe, no FTS infra) fused with vector results via **Reciprocal Rank Fusion**.
- **1.4 LLM reranker** — listwise rerank over the fused pool using the existing OpenRouter model (no new service); fully fallback-safe.

**Design principle throughout:** every change is **additive and fallback-safe** — if pgvector isn't enabled, an LLM call fails, or a note isn't sanitized yet, the system degrades gracefully instead of breaking. Verified via compile checks, role-gating tests, and real-data retrieval tests on a copy of the database.

## 6. Local vs. Railway
Runs fully on **localhost (SQLite)** today — vector (Python), keyword, RRF, rerank, and sanitization all work with your OpenRouter key. **pgvector acceleration** activates automatically only on **Railway (Postgres)** after deploy + a one-time vector backfill.

## 7. Next (planned)
**Phase 2** — grounded/evidence layer (citation verification, calibrated "I don't know"). **Phase 3** — real web/trend tools (agentic tool loop). **Phase 4** — multimodal ingestion (images, recordings, emails).
