---
description: Internal — Local-mode FTS5 full-text search over `documents`. No vectors, no cross-project federation.
---

# local/wiki-search (internal)

Local-mode replacement for the Memex `wiki-search` recipe. Pure SQLite
FTS5 — no Librarian, no embeddings, no re-ranker.

## When to read this

You are running in Local mode and need to look up a document by free-form
text. Examples: "find the OAuth design doc", "show me decisions about
billing", "what does the kickoff meeting say about the schema?"

## Recipe

Call `scripts.backend_local.find_documents(...)`. It runs an FTS5 MATCH
against the `documents_fts` virtual table and joins back to `documents`
for the full row payload:

```sql
SELECT documents.*
FROM   documents
JOIN   documents_fts ON documents.id = documents_fts.rowid
WHERE  documents_fts MATCH ?
  [AND documents.domain = ?]
LIMIT  ?;
```

### Signature

```python
backend_local.find_documents(
    *,
    query: str,                    # FTS5 query string (supports operators)
    project_id: int | None = None, # currently advisory; metadata-scoped
    domain: str | None = None,     # filter to one domain
    limit: int = 10,
) -> list[dict]
```

Each returned dict is a raw `documents` row including `metadata` (still
JSON-encoded string — caller decodes if needed) and `raw_path`. To read
the original body, open `raw_path` from disk.

## FTS5 query tips

- Phrase match: `"OAuth refresh"` (quoted).
- Prefix: `oauth*`.
- Boolean: `oauth AND refresh`, `oauth NOT v1`.
- Column filter: `title:OAuth`. Indexed columns are `key`, `title`,
  `searchable`.
- Missing matches return `[]` — the test contract relies on this.

## Limitations (read before promising the user a result)

- **No vector retrieval.** Semantic-only queries that share no surface
  tokens with the corpus will miss. Reach for substring/phrase hints
  instead.
- **No cross-project search.** FTS5 lives in the project-local
  `<workspace>/.atelier/atelier.db`. Other projects' docs are invisible.
- **No re-ranking beyond raw FTS5 BM25.** Order is "best-FTS5-score
  first" within a single query; cross-query relevance is not stable.
- **No deletes feed the FTS index.** If you ever delete a row from
  `documents`, also `DELETE FROM documents_fts WHERE rowid = ?` (the
  insert trigger is one-directional in the v1.1.0 schema).
