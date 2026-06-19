# Google Drive / Docs Access — Setup Guide

Your CRM stores key documents (pitch decks, internal memos) as **links** in Attio fields
like `pitch_deck`, `internal_docs`, and `working_doc`. The platform fetches the content
behind those links and indexes it so the assistant can use it.

- **Public / "anyone with the link" / published** docs → fetched automatically, **no setup**.
- **Private** docs (shared only inside your Google Workspace) → require a **service account**
  to be granted access. This is a one-time setup. Google's permissions make this mandatory —
  no automated system can read a private file without being granted access.

Once set up, ingestion is **automatic**: it runs after every CRM sync and can also be
triggered manually with the **"Ingest Google docs"** button on Admin → CRM Ventures.

---

## One-time setup for private documents

### 1. Create a Google service account
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) → create or select a project.
2. **APIs & Services → Library** → enable the **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → Service account.**
4. Give it a name (e.g. `intelligence-platform-reader`), create it.
5. Open the service account → **Keys → Add key → Create new key → JSON**. Download the JSON file.
6. Note the service account's **email** (looks like `…@<project>.iam.gserviceaccount.com`).

### 2. Share your Drive documents with it
- The simplest, most "automatic" option: put the relevant docs in a **shared Drive / folder**
  and share that folder with the service account email as **Viewer**.
- Anything in that folder is then readable — including any future docs you add. No per-file work.
- (If docs are scattered, you can share them individually, but a shared folder is far easier.)

### 3. Give the platform the key
Set the key JSON as an environment variable.

**Local:**
```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
# then restart the server
```

**Railway:** add a variable `GOOGLE_SERVICE_ACCOUNT_JSON` and paste the full JSON as its value.

Also ensure the dependency is installed (already in `requirements.txt`):
```bash
pip install -r requirements.txt   # adds google-auth
```

### 4. Run ingestion
- Click **Admin → CRM Ventures → "Ingest Google docs"**, or just run a **CRM sync** (it now
  ingests Drive docs automatically afterward).
- Each link's result is tracked in the `external_documents` table:
  `fetched` (success), `no_access` (private + not shared with the service account),
  `unsupported` (a Drive *folder* link — see note), or `failed`.

---

## How access maps to roles (privacy preserved)
Fetched documents are indexed like CRM notes and run through the same guardrails:
- **Admins** see the full document text.
- **Company users / LPs** see the **sanitized** copy (financials, investment amounts, and
  personal info removed; substance kept). A doc with no shareable content is withheld.

## Notes & limitations
- **Folders:** a `drive.google.com/drive/folders/…` link is marked `unsupported` for now —
  listing a folder's files needs an extra Drive API call (easy follow-on). Sharing the folder
  with the service account still makes the individual *file* links inside it fetchable.
- **Re-fetching:** ingestion re-fetches on each run, so updated docs get re-indexed.
- **No credentials = no private docs:** without the service account, private links simply
  show `no_access` and are skipped — nothing breaks, you just won't see those docs until set up.
