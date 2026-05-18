# Atelier ↔ Memex Index search shims

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md §6.10](../../docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md)
**Memex version verified against:** v2.5.1 (`~/apps/memex` @ HEAD on 2026-05-17)

> The v2.4.1→v2.5.1 deltas were installer hardening (Step 0.2 auto-bootstrap, `scripts/paths.py`, install lock), embedding-error typing, and the registry-reserved-namespace fix. None touched `internal/index/search/SKILL.md` or `scripts/agents/reference_librarian.py`, so the three answers below stand as written.
**Status:** All three §6.10 capability questions resolved. Plan 2 Task 3 (reads) is **unblocked**.

This file records the answers to the three Wave-0 precondition questions the spec named at §6.10 and documents the workarounds Atelier uses when a capability is missing.

---

## Q1 — Structured filters on `documents.metadata` JSON

**Answer:** **NO — not supported by `memex:index:search`.**

### Evidence

- `memex/internal/index/search/SKILL.md` Notes: "Filters supported: `domain` (e.g., `"article"`, `"decision"`), `store` (e.g., `"article"`, `"atelier-projectX"`)." No other filters listed.
- `memex/scripts/agents/reference_librarian.py:execute_query_plan` (lines 116–194) hardcodes only `filters.get("domain")` and `filters.get("store")`. Any other key on `plan["filters"]` is silently ignored.
- The `documents.metadata` column is `TEXT` (`db/index.sql:13`) — SQLite has `json_extract` available, but no caller wires it through.

### Shim — Atelier-side metadata filter

The Index is itself a registered Memex Core store (`install.py` calls `registry.register_store("index", ...)`), so Atelier can query it directly via **`memex:core:query`** with a `json_extract`-based WHERE clause:

```python
# scripts/backend_memex.py
def find_documents_by_metadata(*, project_id: int, domain: str | None = None,
                                limit: int = 10) -> list[dict]:
    """Cross-table metadata filter — used when the spec asks for
    'all tasks part_of project X' kind of queries that index:search
    can't filter natively."""
    sql = """
        SELECT index_id, key, domain, store, table_name, row_id, searchable
        FROM documents
        WHERE json_extract(metadata, '$.project_id') = ?
    """
    params: list = [project_id]
    if domain:
        sql += " AND domain = ?"
        params.append(domain)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return memex_core.query(name="index", sql=sql, params=tuple(params))
```

**Atelier writes metadata as JSON during Tier 2 writes** (per spec §6.2), so `metadata.project_id`, `metadata.priority`, etc. are reliably present and queryable.

**Trade-off:** This is a second query (FTS5 hit ➜ Atelier-side filter) when both relevance ranking AND metadata filtering are needed. The spec acknowledges this as "documented limitation" in §6.10. Acceptable: cross-project recall in Memex mode is a power-user query, not the hot path.

### Optional upstream fix

If/when Memex v2.5+ adds a `metadata_filter` field to the query-plan schema (`{"path": "$.project_id", "op": "=", "value": <X>}`) and threads it into `execute_query_plan`, Atelier swaps the shim for the native filter without touching any caller. Not raised as a blocker against Memex — the shim has acceptable performance characteristics and the cross-plugin demand for metadata filtering is unproven.

---

## Q2 — `relations` traversal in `memex:index:search`

**Answer:** **NO — `index:search` never reads the `relations` table.**

### Evidence

- `reference_librarian.execute_query_plan` (memex `scripts/agents/reference_librarian.py:116–194`) joins `documents_fts` to `documents` only. There is no `relations` join in either the FTS path or the vector path.
- The query-plan schema (per `prompts/reference_librarian.md` — referenced from `build_prompt`) accepts `fts_query`, `vector_query`, `filters`, `limit`. No `relations` filter.
- The `relations` table exists (`db/index.sql:28–35`), is populated by `librarian.write_entry` on Tier 2 writes (when callers supply relations), and has indexes on both directions (`PRIMARY KEY (from_index_id, to_index_id, rel_type)` + `relations_to_idx`). It is **stored** for cross-document graphs, but Memex's search path does not consume it.

### Shim — direct `memex:core:query` against `index.db.relations`

Same trick as Q1 — `index` is a registered store, so:

```python
# scripts/backend_memex.py
def find_related(*, from_index_id: str, rel_type: str | None = None,
                  direction: str = "outbound") -> list[dict]:
    """Traverse one hop of the relations graph from a known document.

    direction='outbound' → edges WHERE from_index_id = ? (what does X point to?)
    direction='inbound'  → edges WHERE to_index_id   = ? (who points to X?)
    """
    col = "from_index_id" if direction == "outbound" else "to_index_id"
    other = "to_index_id"  if direction == "outbound" else "from_index_id"
    sql = f"""
        SELECT r.{other} AS index_id, r.rel_type, r.confidence,
               d.key, d.domain, d.store, d.table_name, d.row_id, d.searchable
        FROM relations r
        JOIN documents d ON d.index_id = r.{other}
        WHERE r.{col} = ?
    """  # nosec B608 — col/other are hard-coded enum values
    params: list = [from_index_id]
    if rel_type:
        sql += " AND r.rel_type = ?"
        params.append(rel_type)
    return memex_core.query(name="index", sql=sql, params=tuple(params))
```

Two-hop and longer traversals (rare in Atelier — `task → project → workspace` is the deepest practical query) use multiple `find_related` calls in Python; Atelier does not push them into one CTE because none of the §12 test scenarios need it.

### Use cases this unblocks

- **Supersedes chain:** `find_related(from_index_id=<doc_id>, rel_type="supersedes")` walks the edit history of a document.
- **Tasks of a project:** `find_related(from_index_id=<project_index_id>, rel_type="part_of", direction="inbound")` (with a domain filter at the join level if needed).
- **Cross-references on retrospectives:** `find_related(from_index_id=<postmortem_id>, rel_type="recaps")` finds the original incident meeting.

### Optional upstream fix

The natural Memex extension is a `relations_filter` field in the query plan: `{"from_index_id": "...", "rel_type": "...", "direction": "outbound|inbound"}` filtered before FTS5 ranking. Not pursued upstream for the same reason as Q1.

---

## Q3 — `documents.key` uniqueness invariant

**Answer:** **YES — `documents.key` has a `UNIQUE` index. Globally unique within `index.db`.**

### Evidence

- `memex/db/index.sql:26`:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS documents_key_unique_idx ON documents(key);
  ```
- The schema comment is explicit (lines 21–25):
  > "Exact-match uniqueness invariant on `key`. SQLite treats NULLs as distinct, so unkeyed rows remain unconstrained. The Librarian prechecks this index before INSERT and raises a typed `DuplicateKeyError` on collision; the UNIQUE index is the last-line defense for any code path that bypasses the precheck."

### What this means for Atelier

Atelier's key format from spec §6.7 is:

```
<workspace_slug>/<project_slug>/<domain>/<date>-<title_slug>-<seq>
```

The `seq` allocator (smallest unused integer ≥ 1 for the same prefix) is what guarantees Atelier's keys satisfy the UNIQUE constraint. **The spec's design is correct as written** — no key change required.

### Hard contracts that follow from the UNIQUE constraint

These are commitments Atelier's Tier 2 write path must honor (per spec §6.7 + §6.8):

1. **Content edits MUST get a fresh `key`.** Spec §6.8 says "every content edit as a fresh `librarian.write_entry` call (new `index_id` row) … the prior `index_id` row remains for citation stability, and the new row supersedes via `relations` (`rel_type="supersedes"`)." Since `key` is UNIQUE in `documents`, the prior row's `key` is in the table — the new row needs a **different** key. Atelier's `seq` does this for free: same-day same-title edits land at `seq=2, 3, 4…`, so keys diverge. Cross-day edits diverge via the `<date>` component.

2. **The seq allocator must look up against `index.db.documents`, not against Atelier's own tables.** The UNIQUE constraint spans every store — if another consumer (Brain, future plugin) ever wrote a key in Atelier's namespace, Atelier's seq query needs to see it. Implementation: `memex:core:query name="index" sql="SELECT key FROM documents WHERE key LIKE '<prefix>%'"`. One read per Tier 2 write; acceptable.

3. **Atelier-namespace prefix isolation.** Atelier's keys begin with `<workspace_slug>/<project_slug>/<domain>/…`, which is structurally distinct from Brain's keys (likely `<source>/<hash>` or similar). No real-world collision risk between consumers; the prefix scan is fast even with no further partitioning.

4. **`DuplicateKeyError` recovery.** If a Tier 2 write races (theoretically possible under multi-process Atelier — though spec §6.7 documents "race-free under single-process Atelier"), `librarian.write_entry` will raise `DuplicateKeyError`. Atelier increments `seq` and retries. Bounded retry count = 5; beyond that, surface the error.

---

## Summary — Plan 2 Task 3 unblock status

| § | Capability | Status | Atelier action |
|---|---|---|---|
| Q1 | `metadata` JSON filtering in `index:search` | **Shim** | Atelier-side via `memex:core:query name="index"` with `json_extract`. Acceptable perf; no upstream blocker. |
| Q2 | `relations` traversal in `index:search` | **Shim** | Atelier-side via `memex:core:query name="index"` joining `documents` to `relations`. Acceptable perf; no upstream blocker. |
| Q3 | `documents.key` uniqueness | **Confirmed YES** | Spec §6.7 seq allocator is correct as written. Honor the four contracts in this doc's Q3 section. |

**Plan 2 Task 3 (reads) is unblocked.** The implementing engineer writes two read paths:

1. **Primary:** dispatch through `memex:run` → `memex:index:search` for natural-language queries (FTS5 + optional vector). Filters limited to `domain` and `store`.
2. **Atelier-specific reads:** call `memex:core:query name="index" sql=...` directly for the structured filters (metadata, relations) the spec's §12 test surface demands.

Both paths return Atelier's standard result dict shape `{index_id, key, domain, store, table_name, row_id, searchable, ...}`. Hydration to full target-store rows is the caller's responsibility (per `memex:index:search`'s "Step 5 — Return raw results" contract).

---

## Notes for future maintenance

- If Memex extends `execute_query_plan` to support `metadata` filters or `relations` joins natively, drop the shim and switch to the query-plan field. Atelier's caller surface (`backend.find_documents`, `backend.find_related`) is unaffected — the change is internal to `backend_memex.py`.
- If Memex's `documents.key` UNIQUE constraint is ever relaxed (unlikely — it's load-bearing across the Index), the seq allocator becomes optional but harmless.
- **v2.5.1 contract: `backend_memex.py` does NOT need client-side `__*` filtering** when iterating results from `registry.list_stores()` or store enumeration. Memex's `registry` API reserves the `__dunder__` namespace at the API layer (`register_store` raises `ValueError`, `list_stores` filters, `get_store` returns `None`, `unregister_store` no-ops). Per the v2.5.1 CHANGELOG: "Downstream consumers (Atelier's `backend_memex.py`) can now drop client-side `__*` filtering." When the Plan 2 implementer reads stores or related metadata from Memex, trust the upstream contract.
- This shim doc lives in `internal/memex/` (not user-discoverable as a slash command) — it is engineer-facing documentation that Plan 2 Task 3's implementer reads when wiring the read path.
