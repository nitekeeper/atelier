---
description: Internal — first-run Atelier bootstrap into Memex. Seeds Atelier's roles + agent profiles into ~/.memex/agents.db, provisions the atelier store via memex:core:create-store, and writes the bootstrap marker. Idempotent.
---

# bootstrap-memex (internal)

> **Prerequisites**
> - Mode: **MEMEX ONLY** — `mode_detector.detect_mode()` must return `"memex"` or this procedure is a no-op
> - Required: Memex plugin installed in Claude Code; `~/.memex/` bootstrapped (i.e. `~/.memex/registry.json` must exist — either via `memex:run` Step 0.2 auto-bootstrap or a prior `memex:run` invocation)
> - Required tables: created/seeded by this skill — `~/.memex/atelier.db` registered as a Memex Core store (via `memex:core:create-store`); roles + agent profiles seeded into `~/.memex/agents.db` (Memex's pre-existing agents DB)

## When invoked

Every Atelier command in Memex mode reads
`memex_home() / "atelier.bootstrap.json"` at startup. If the marker is
missing or the recorded `version` is older than the installed Atelier
version, this procedure runs. After it succeeds the marker is rewritten
and subsequent commands skip the body in O(1).

## Preconditions

1. `mode_detector.detect_mode() == "memex"`. If not, bootstrap is a no-op
   — Local mode runs its own provisioning (`internal/local/...`).
2. **Memex itself must be bootstrapped.** Atelier cannot seed onto an
   uninitialized Memex (`~/.memex/registry.json` must exist). Two
   acceptable behaviors per spec §5 step 0:
   - **Preferred:** the first Atelier touch dispatches through
     `memex:run` so memex v2.5.0+'s Step 0.2 auto-bootstrap fires.
   - **Acceptable:** atelier prechecks `(memex_home() / "registry.json").exists()`
     and surfaces a clear user message when absent.

   The raw `scripts.db.require_bootstrap()` floor check raises
   `MemexNotInitializedError` with operator guidance — Atelier must
   catch this and reformat (NEVER let it propagate as a crash to the user).

## Recipe

```python
import datetime
import importlib.metadata as md
import json
import sqlite3 as _sqlite3
from pathlib import Path

from scripts import backend, mode_detector, seed_data
from scripts import backend_memex

# 1. Mode floor — bootstrap is Memex-only.
assert mode_detector.detect_mode() == "memex", "bootstrap-memex called in non-Memex mode"

# 2. Make Memex importable, then run the floor check.
backend_memex._ensure_memex_importable()
from scripts import db as memex_db                # type: ignore  # noqa: E402
from scripts import roles as memex_roles          # type: ignore  # noqa: E402
from scripts import agents as memex_agents        # type: ignore  # noqa: E402
from scripts import stores as memex_stores        # type: ignore  # noqa: E402
from scripts import registry as memex_registry    # type: ignore  # noqa: E402

try:
    memex_db.require_bootstrap()                  # raises if ~/.memex/registry.json absent
except memex_db.MemexNotInitializedError as e:
    raise RuntimeError(
        "Memex is installed but not initialized. Run `memex:run` once "
        "to trigger Step 0.2 auto-bootstrap, then re-run your Atelier "
        "command."
    ) from e

memex_home_path = memex_db.memex_home()
agents_db = str(memex_home_path / "agents.db")

# 3. Read marker; skip if version matches.
marker_path = memex_home_path / "atelier.bootstrap.json"
try:
    atelier_version = md.version("atelier")
except md.PackageNotFoundError:
    atelier_version = "0.0.0-dev"
if marker_path.exists():
    try:
        prior = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        prior = {}
    if prior.get("version") == atelier_version:
        return                                    # already current; nothing to do

# 4. Seed roles via the idempotent helper (memex:core:register-role under the hood).
for r in seed_data.load_role_seed():
    backend.find_or_create_role(name=r["name"], description=r["description"])

# 5. Seed agents via the idempotent helper (memex:core:register-agent under the hood).
#    seed_data.load_agent_seed() iterates the full templates/agents/ directory
#    (~50 personas) — one record per .json file.
#    Prerequisite: `backend.find_or_create_role` and `backend.find_or_create_agent`
#    must be implemented (Plan 2 Task 1 / Task 8). This file documents the
#    bootstrap shape; T6 / Plan 2 Task 10 wires them up.
role_map = {row["name"]: row["id"] for row in memex_roles.list_roles(agents_db)}
for a in seed_data.load_agent_seed():
    backend.find_or_create_agent(
        agent_id=a["agent_id"],
        name=a["name"],
        role_id=role_map[a["role_name"]],
        profile=a["profile"],
    )

# 6. Provision the atelier store if absent. Uses memex:core:create-store
#    with the real signature create_store(name, path, migrations_dir, schema_version="v1")
#    (see memex/scripts/stores.py:21). registry.json is a flat map, so
#    get_store("atelier") is the right idempotency probe.
if memex_registry.get_store("atelier") is None:
    atelier_plugin_root = Path(__file__).resolve().parents[2]   # plugin root
    atelier_db_path = str(memex_home_path / "atelier.db")
    memex_stores.create_store(
        name="atelier",
        path=atelier_db_path,
        migrations_dir=str(atelier_plugin_root / "migrations" / "shared"),
        # schema_version defaults to "v1"; Memex Core tracks applied migrations
        # in the store's own `migrations` table, so create_store is idempotent
        # across re-runs (new migrations replay; already-applied ones skip).
    )

# 7. Write the marker.
marker_path.write_text(
    json.dumps(
        {
            "version": atelier_version,
            "bootstrapped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        indent=2,
    ),
    encoding="utf-8",
)
```

## How the helpers map to Memex ops

| Atelier helper | Memex op | Memex Python |
|---|---|---|
| `backend.find_or_create_role(name=, description=)` | `memex:core:register-role` (with precheck) | `roles.list_roles(agents_db)` then `roles.create_role(agents_db, name, description)` |
| `backend.find_or_create_agent(agent_id=, name=, role_id=, profile=)` | `memex:core:register-agent` (with precheck) | `agents.get_agent(agents_db, agent_id)` then `agents.create_agent(agents_db, agent_id, name, role_id, profile)` |
| `memex_stores.create_store(name=, path=, migrations_dir=)` | `memex:core:create-store` | `stores.create_store(name, path, migrations_dir, schema_version="v1")` |
| `memex_db.require_bootstrap()` | (none — internal floor check) | raises `MemexNotInitializedError` |

Both `find_or_create_role` and `find_or_create_agent` are atelier-side
idempotent wrappers (see `scripts/backend.py` lines 176–186 stubs).
Implementation pattern per spec §5:

```python
def find_or_create_role(*, name, description):
    existing = [r for r in memex_roles.list_roles(agents_db) if r["name"] == name]
    if existing:
        return existing[0]
    try:
        return memex_roles.create_role(agents_db, name=name, description=description)
    except _sqlite3.IntegrityError:
        # Race: another writer seeded the same role between list + create.
        # Re-read and return the now-present row.
        return next(r for r in memex_roles.list_roles(agents_db) if r["name"] == name)
```

The same precheck-then-create pattern (with `agents.get_agent` as the
probe and `agents.create_agent` as the writer) is the body of
`find_or_create_agent`. Both swallow `sqlite3.IntegrityError` only after
a positive list-or-get probe — never blind.

## Idempotency

- **Marker:** version-pinned — re-bootstrap only on Atelier upgrade.
- **Roles:** `find_or_create_role` pre-reads `list_roles(agents_db)`;
  duplicate-on-race swallowed via `try / except sqlite3.IntegrityError`
  followed by a re-read.
- **Agents:** `find_or_create_agent` probes via `get_agent(agents_db, agent_id)`;
  same `IntegrityError` guard for races.
- **Store:** skipped if `memex_registry.get_store("atelier")` returns
  a non-None record (registry.json is a flat `{name: record}` map).
- **Migrations inside the store:** `memex:core:create-store` rolls
  the store's own `migrations` table forward; already-applied scripts
  are no-ops.

## Failure semantics

If any step raises, the marker is **not** written. The next Atelier
command will retry from step 4. Partial state (e.g., 4 of 6 roles
seeded, store not yet created) is acceptable — the re-run skips
already-seeded entries and resumes at the failing step.

The only unrecoverable error is `MemexNotInitializedError` from
`require_bootstrap()`. Reformat per the precondition section above —
do NOT propagate the raw exception.

## Hard invariants

- **Never call `roles.create_role` or `agents.create_agent` without a
  precheck.** Both raise `sqlite3.IntegrityError` on duplicates; the
  precheck-then-create pattern (encapsulated in `find_or_create_role` /
  `find_or_create_agent`) is the only safe shape.
- **Never bypass `memex:core:create-store` to provision `atelier.db`.**
  Doing so skips registry registration and the universal `migrations`
  table.
- **Never write the marker before all steps succeed.** The marker is the
  signal that subsequent commands can skip bootstrap; a premature write
  hides a half-seeded state.
