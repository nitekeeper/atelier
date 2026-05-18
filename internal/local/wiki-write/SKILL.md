---
description: Internal — Local-mode document write. FTS5-indexed via `documents_fts`; raw body archived to `<workspace>/.atelier/raw/`.
---

# local/wiki-write (internal)

Local-mode replacement for the Memex `wiki-write` recipe. No Librarian, no
embeddings, no federated Index — just project-local SQLite + FTS5 + a
content-addressed raw archive on disk.

## When to read this

You are running in Local mode (Memex absent) and need to persist any
document-shaped payload — design notes, plans, meeting minutes, project
descriptions, anything searchable.

## Recipe

Call `scripts.backend_local.write_document(...)` exactly once per
document. It performs three side-effects atomically per call:

1. **Archive the raw body.** Computes a sha256 over `body`, slugifies
   `title`, and writes the bytes to
   `<workspace>/.atelier/raw/<2char-hash>/<canonical_key>.md`. Idempotent
   on content hash — re-archiving identical bytes is a no-op. See
   `internal/local/wiki-archive/SKILL.md`.
2. **Insert one row into `documents`.** Columns: `key`, `domain`,
   `title`, `searchable`, `raw_path`, `metadata` (JSON), `created_by`,
   `created_at`. `searchable` is `title + "\n\n" + body + metadata
   string values`, untruncated (spec §6.8).
3. **Auto-index in `documents_fts`.** The `documents_ai` AFTER INSERT
   trigger copies `(key, title, searchable)` into the FTS5 virtual table
   `documents_fts`. No manual FTS write required.

### Signature

```python
backend_local.write_document(
    *,
    domain: str,            # "design" | "task" | "meeting" | "project" | ...
    title: str,
    body: str,
    metadata: dict,         # JSON-serializable; project_id should live here
    caller_agent_id: str,
    source_url: str | None = None,
) -> dict
```

Return shape (parity with Memex backend so the facade dispatcher is
mode-agnostic):

```python
{"status": "ingested", "index_id": None, "row_id": <int>,
 "key": <slug>, "domain": domain, "relations": []}
```

`index_id` is **always None** in Local mode — there is no federated
index. Callers that condition on `index_id` must treat None as the
local-success sentinel.

## Hard rules

- Never write to `documents_fts` directly. The trigger handles indexing.
- Never bypass `_archive_raw` and INSERT a row with `raw_path=NULL`. The
  archive path is the only recovery surface if the DB is wiped.
- Never truncate `searchable`. FTS5 needs the full corpus per spec §6.8.
- The caller must already hold a valid `caller_agent_id` (FK to
  `agents.id`). Mint it via `backend_local.find_or_create_agent` first if
  unsure.
