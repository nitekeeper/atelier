---
description: Internal — routes Atelier operational-state CRUD through Memex Core (insert / update / query / register-role / register-agent / get-agent). Bypasses the Librarian; pure SQL. Not user-visible.
---

# memex/dispatch-core (internal)

## When invoked

An Atelier operation needs to write or read an operational row that
either has no `index_id` column (sessions, phase rows, bypasses) or
mutates a field that the Index's `searchable` does not cover (task
status flip, phase transition, assignee change). These are spec §6.1
**Tier 1** writes — pure CRUD, no Librarian, no Archivist, no embeddings.

Also covered: bootstrap-time role/agent seeding into `~/.memex/agents.db`
(the machine-global agents store, shared with every Memex consumer) and
agent profile lookups.

## Atelier operations that route here

| Atelier op | Memex surface | Target |
|---|---|---|
| `update_task_status` (status flip only) | `memex:core:update` | `atelier.tasks` |
| `transition_phase` | `memex:core:update` | `atelier.projects` |
| `record_phase_bypass` | `memex:core:insert` | `atelier.phase_bypasses` |
| `upsert_session` | `memex:core:insert` / `memex:core:update` | `atelier.sessions` |
| `add_meeting_participant` | `memex:core:insert` | `atelier.meeting_participants` |
| `assign_task` (assignee change only) | `memex:core:update` | `atelier.tasks` |
| Read by primary key (`get_task`, `get_project`) | `memex:core:query` | `atelier.<table>` |
| Bootstrap: seed roles | `memex:core:register-role` `*` | `agents.roles` |
| Bootstrap: seed agents | `memex:core:register-agent` `*` | `agents.agents` |
| Agent profile lookup (e.g., resolve `tasks.assigned_to`) | `memex:core:get-agent` | `agents.agents` |
| Raw SQL inside a transaction (multi-row migrations, audits) | `memex:core:execute` `*` (see note) | `atelier.*` |

> `*` register-* is Atelier shorthand — precheck-then-create is required;
> raw `memex.roles.create_role` / `memex.agents.create_agent` raise
> `sqlite3.IntegrityError` on collision. See `backend.find_or_create_role`
> / `backend.find_or_create_agent` wrappers. Likewise `memex:core:execute`
> is Atelier shorthand for raw SQL that doesn't fit
> `insert`/`update`/`delete`/`query`; Memex v2.5.1 does not expose a
> public `execute` op (see note below).

> Note on `memex:core:execute`: Memex v2.5.1 does NOT expose a public
> `execute` op — `scripts/stores.py` ships `query`, `insert`, `update`,
> and `delete`. When this procedure says `memex:core:execute`, it is the
> Atelier-side shorthand for "raw SQL that doesn't fit
> insert/update/delete/query". For SELECTs use `memex:core:query`; for
> multi-statement transactions or DDL, open a direct connection through
> `backend_memex._memex_core_conn(name="atelier")` (a thin wrapper that
> calls `stores.connect(name)` and enforces WAL + FK pragmas). When
> Memex publishes a real `memex:core:execute` op (proposed for v2.6),
> drop the shorthand and route through that instead.

## Recipe

The procedure body is `scripts.backend_memex._memex_core_*` helpers.
They:

1. Import Memex's `scripts.stores` and `scripts.agents` modules from the
   installed plugin via `backend_memex._ensure_memex_importable()`.
2. Run the operation against the `atelier` store registered in
   `~/.memex/registry.json` (or `agents.db` for roles/agents/get-agent).
3. Return a list of dict rows (`query`) or the affected row
   (`insert`/`update`).

### Insert — `memex:core:insert`

```python
from scripts import stores as memex_stores
row = memex_stores.insert(
    name="atelier",                  # registered store
    table="phase_bypasses",          # target table
    row={"project_id": 7, "skill": "dev-design", ...},
)
# returns {"id": <new_pk>, **row}
```

### Update — `memex:core:update`

```python
from scripts import stores as memex_stores
row = memex_stores.update(
    name="atelier",
    table="tasks",
    row_id=42,                       # integer PK only — by design
    updates={"status": "in_progress"},
)
# returns the updated row dict, or None if row_id absent
```

`row_id` is the **integer** primary key. For tables keyed by a TEXT PK,
fall through to `memex:core:execute` (see note). Atelier's v1.1.0 schema
has integer PKs throughout; this fall-through is reserved for hypothetical
future TEXT-PK tables. The `agents.id` TEXT case lives in
`~/.memex/agents.db` (not the atelier store) and is covered by
`register-agent` / `get-agent` rows in the routing table above — it does
NOT route through `memex:core:update`.

### Query — `memex:core:query`

```python
from scripts import stores as memex_stores
rows = memex_stores.query(
    name="atelier",
    sql="SELECT * FROM tasks WHERE project_id = ? AND status != ?",
    params=(project_id, "done"),
)
# rows: list[dict]
```

Always use bound parameters (`?` + tuple). No string-formatting SQL.

### Raw SQL — `memex:core:execute` (Atelier shorthand)

Until Memex publishes a public `execute` op, use the connection wrapper:

```python
from scripts import backend_memex
with backend_memex._memex_core_conn(name="atelier") as conn:
    conn.execute(
        "UPDATE tasks SET assigned_to = ? WHERE assigned_to = ?",
        (new_agent_id, old_agent_id),
    )
    conn.commit()
```

Use this only when the operation cannot be expressed via
`insert` / `update` / `query` (e.g., multi-row UPDATE, audit queries,
JSON1 expressions on `documents.metadata`).

### Roles — `memex:core:register-role`

```python
from scripts import roles as memex_roles
from scripts.db import memex_home
agents_db = str(memex_home() / "agents.db")
row = memex_roles.create_role(agents_db, name="Product Manager",
                              description="Owns scope and priorities.")
# returns {"id": <new_pk>, "name": ..., "description": ...}
```

`roles.name` is `UNIQUE`. A duplicate raises `sqlite3.IntegrityError`;
**no silent no-op**. Callers must precheck via `roles.list_roles(...)` or
use Atelier's idempotent helper `backend.find_or_create_role(...)`.

### Agents — `memex:core:register-agent`

```python
from scripts import agents as memex_agents
row = memex_agents.create_agent(agents_db, agent_id, name, role_id, profile)
# returns {"agent_id": ..., "name": ..., "role_id": ..., "profile": ...}
```

`agents.id` is `TEXT PRIMARY KEY`. Duplicate raises
`sqlite3.IntegrityError`. Precheck via `agents.get_agent(agents_db, agent_id)`
or use `backend.find_or_create_agent(...)`.

### Agent lookup — `memex:core:get-agent`

```python
record = memex_agents.get_agent(agents_db, agent_id)
# record: dict | None
```

Resolves `tasks.assigned_to`, `meetings.created_by`, etc. Returns `None`
on missing — caller's responsibility to handle.

## When NOT to use this procedure

If the row carries searchable narrative content (task description,
meeting summary, document body, project description), route through
`internal/memex/dispatch-write/SKILL.md` instead — that puts the row
in the federated Index where `memex:brain:ask` can find it. Tier 1
mutations are status flips and operational state only.

Rule of thumb: **does the change alter what `documents.searchable` would
say?** If yes → Tier 2 (`dispatch-write`). If no → Tier 1 (this file).

## Errors

| Exception | Cause | Recovery |
|---|---|---|
| `ValueError: Unknown store: atelier` | Bootstrap has not run. | Run `internal/bootstrap-memex/SKILL.md`. |
| `sqlite3.IntegrityError` (roles/agents) | Duplicate `name` or `agent_id`. | Use the `find_or_create_*` helpers. |
| `sqlite3.OperationalError: no such table` | Atelier migrations not applied. | Re-run bootstrap; it replays `migrations/shared/`. |
| `RuntimeError: Memex plugin not found` | Mode detector stale. | `mode_detector._clear_cache()`; re-detect; fall back to Local. |

## Hard invariants

- **No `searchable`-affecting write goes through this procedure.** That
  would silently desynchronize the Index from the target store. Tier 2
  (dispatch-write) is the only correct path for content changes.
- **All SQL uses bound parameters.** No format-string interpolation.
- **All connections obtained via `memex_stores.*` or `_memex_core_conn`.**
  Never open raw `sqlite3.connect()` against `atelier.db` — those bypass
  WAL and FK pragmas.
