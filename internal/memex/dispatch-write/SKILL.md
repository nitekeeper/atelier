---
description: Internal — Tier 2 (structured-row) Atelier writes through memex:index:write with a caller-built librarian_output. No Librarian subagent dispatch, no LLM. Not user-visible.
---

# memex/dispatch-write (internal)

## When invoked

An Atelier business operation (create project, write meeting minutes, new
task, content edit on an existing project_documents row) needs an
Atelier-domain row indexed in Memex's federated Index. Atelier knows the
`domain` (per `scripts/domain_vocabulary.DOMAINS`) and can compose
`searchable` from the structured row deterministically — so this is
spec §6.2 **Tier 2**: a caller-built `librarian_output`, NO Librarian
subagent dispatch, NO LLM call.

For Tier 3 (prose ingest where the domain and relations must be extracted
from text) the Librarian subagent must be dispatched via the Task tool —
that is `memex:index:write`'s "dispatch path" (`librarian_output=None`).
The corresponding Atelier internal procedure (`internal/memex/dispatch-ingest`)
is **deferred to a future plan**. Until `internal/memex/dispatch-ingest` lands, callers needing Tier 3 must invoke `memex:run` directly with an `ingest` intent. There is no other supported path.

## The two paths of `memex:index:write`

`memex:index:write` (memex v2.2.0+) branches at Step 0 on the
`librarian_output` argument:

| Caller supplies `librarian_output=` | Path taken | Cost |
|---|---|---|
| a validated dict (caller-built path) | skip Steps 1–3, validate + write | one Index INSERT + one target-store INSERT + one embedding call |
| `None` (dispatch path) | Build prompt → Task-tool Librarian subagent → parse → write | adds one LLM call |

Atelier's Tier 2 writes always take the caller-built path. We make this
explicit at the call site with a sentinel constant
`mode="callerbuilt"` (Atelier-side convention — there is no `mode=`
kwarg on Memex's API; the branch in `memex:index:write` is driven solely
by whether `librarian_output` is `None`). Carrying the explicit string in
the Python wrapper:

```python
DISPATCH_MODE_CALLERBUILT = "callerbuilt"   # scripts/backend_memex.py

def _atelier_write(*, mode="callerbuilt", ...):
    assert mode == DISPATCH_MODE_CALLERBUILT, \
        "Atelier Tier 2 must never dispatch the Librarian subagent (spec §6.2 invariant)."
    ...
```

…buys two things: (1) callers can grep the constant to find every Tier 2
write site, and (2) tests (§12) can assert no business op ever passes
anything else.

## Inputs

| Field | Type | Notes |
|---|---|---|
| `domain` | str | Must be in `scripts.domain_vocabulary.DOMAINS`. |
| `title`, `body` | str, str\|None | `body` is searchable narrative; full content, no truncation cap. |
| `payload` | dict | Target-table columns (persisted in `~/.memex/atelier.db.<table>`). MUST NOT include `index_id` — `librarian.write_entry` assigns it. |
| `target_table` | str | One of `projects`, `tasks`, `meeting_minutes`, `project_documents`. |
| `caller_agent_id` | str | An Atelier-seeded agent (`atelier-product-manager-1`, etc.). Must exist in `~/.memex/agents.db` — seeded by `internal/bootstrap-memex/SKILL.md`. |
| `metadata` | dict\|None | Written to `index.db.documents.metadata` (JSON). String-valued entries fold into `searchable` (§6.8). May carry `source_ref` for idempotent replay (Plan 4). |
| `relations` | list\|None | `[{"to_index_id": ..., "rel_type": ...}, ...]` for explicit graph edges. The recipe auto-attaches a `part_of` edge to the owning project's Index row when `metadata["project_id"]` is set (§6.9). |

## Recipe

The procedure body is `scripts.backend_memex._atelier_write(...)`. It:

1. `domain_vocabulary.assert_valid(domain)` — rejects unknown domains.
2. Resolves `project_slug` from `metadata["project_id"]` and constructs
   the canonical `key` per spec §6.7:
   `<workspace>/<project>/<domain>/<YYYY-MM-DD>-<title_slug>-<seq>`.
   `seq` is the smallest unused integer ≥ 1 across existing keys with the
   same prefix (Memex v2.3.0+ enforces `UNIQUE` on `documents.key`).
3. Composes `searchable` per spec §6.8 (full body, no truncation cap):
   ```python
   searchable = "\n\n".join(filter(None, [
       title, body or "", metadata_narrative_excerpt(metadata),
   ]))
   ```
   Function name follows spec §6.8 (`metadata_narrative_excerpt`), not
   Plan 2's draft (`metadata_narrative`).
4. Builds the `librarian_output` dict:
   ```python
   from scripts.agents import librarian as memex_librarian
   librarian_output = memex_librarian.validate_output({
       "index_id":   str(uuid.uuid4()),    # memex convention; agents/librarian.py:221
       "key":        canonical_key,        # from step 2
       "domain":     domain,
       "searchable": searchable,
       "metadata":   metadata,
       "relations":  relations,
   })
   ```
   `validate_output` raises `ValueError` on missing required fields
   (`index_id`, `key`, `domain`, `searchable`).
5. Best-effort embedding:
   ```python
   from scripts import embeddings as memex_embeddings
   try:
       embedding = memex_embeddings.encode(librarian_output["searchable"])  # bytes
   except memex_embeddings.EmbeddingUnavailable as e:
       memex_embeddings.log_skip(
           e, caller_agent_id=caller_agent_id,
           index_id=librarian_output["index_id"],
           input_chars=len(librarian_output["searchable"]),
       )
       embedding = None
   ```
   Catch only `EmbeddingUnavailable` (memex v2.4.1 typed contract) — any
   other exception is a real bug and propagates. The skip is audited to
   `~/.memex/audits/embedding-skip-log.md`. FTS5 still works without the
   vector.
6. Persist via the caller-built path of `memex:index:write`:
   ```python
   result = memex_librarian.write_entry(
       payload=payload,
       librarian_output=librarian_output,
       target_store="atelier",
       target_table=target_table,
       caller_agent_id=caller_agent_id,
       embedding=embedding,
   )
   ```
   This is Memex's canonical two-stage write: Index row → target-store
   row → `documents.row_id` backlink. On `librarian.DuplicateKeyError`
   (race on `seq`), bump `seq` by 1 and retry once. Further collisions
   propagate.
7. Returns the dict with `{"status": "ingested", "index_id", "key", "domain", "row_id", "relations"}`.

## Errors

| Exception | Cause | Recovery |
|---|---|---|
| `RuntimeError: Memex plugin not found` | `mode_detector` returned `memex` but the plugin is unimportable now. | `mode_detector._clear_cache()` then re-detect; fall back to Local. |
| `ValueError: unknown domain` | `domain` ∉ `DOMAINS`. | Use one of the vocabulary entries, or amend the spec via `internal/memex/domain-vocabulary.md`. |
| `ValueError: Unknown store: atelier` | Bootstrap has not run. | Run `internal/bootstrap-memex/SKILL.md`. |
| `ValueError: librarian_output missing fields` | Shouldn't happen — `_atelier_write` builds the dict. Indicates a Memex schema bump. | Pin the Memex version requirement and update Atelier. |
| `librarian.DuplicateKeyError` | `documents.key` collision (race on `seq`). | One retry with `seq+1`; propagate after. |
| `embeddings.EmbeddingUnavailable` | Degraded-mode provider miss. | Already handled — write proceeds with `embedding=None`; FTS5 unaffected. |

## `source_ref` contract for idempotent replay

Callers may set `metadata["source_ref"]` to a stable string identifying
the row's origin (e.g. `"atelier:tasks:42"` from Plan 4's
`scripts/migrate_to_memex.py`). `_atelier_write` passes `metadata`
through verbatim into the validated `librarian_output`, so `source_ref`
lands in `~/.memex/index.db.documents.metadata` as a JSON-extractable
field — no code change in the write path.

The matching reverse lookup
`backend_memex.lookup_index_id_by_source_ref(source_ref)` (Plan 2 Task 3,
surfaced through `scripts/backend.py`) queries
`SELECT index_id FROM documents WHERE json_extract(metadata, '$.source_ref') = ?`
so callers can decide whether to re-emit. This is what makes Plan 4's
`migrate_project` safe to rerun after a partial outage — already-replayed
rows count under `already_present` instead of triggering
`librarian.DuplicateKeyError`.

`write_task` and `write_meeting` accept `source_ref` as a top-level
kwarg and fold it into their internal `metadata` dict (Plan 2 Task 1).
`write_document` and `write_project` rely on the caller to include it in
`metadata` directly.

## Hard invariants

- **Never call this with `mode="dispatch"`** (or any value other than
  `"callerbuilt"`). Atelier business operations must not dispatch the
  Librarian subagent — that's the spec §6.2 Tier 2 invariant, tested in §12.
- **`payload` must not include `index_id`.** `librarian.write_entry`
  assigns it from `librarian_output`.
- **`searchable` is the full body.** No truncation. If a substring is
  not in `searchable`, no `memex:brain:ask` query for that substring
  will return the document (§6.8).
