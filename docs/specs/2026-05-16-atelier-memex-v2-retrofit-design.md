# Atelier ↔ Memex v2 Retrofit — Design

**Status:** Draft — pending approval before plan-writing
**Date:** 2026-05-16
**Author:** nitekeeper (with Claude)
**Targets:** Atelier (this repo), Memex v2 (consumer integration only — no Memex-side changes required)

---

## 1. Context and motivation

Atelier was built against Memex v1 — a per-project SQLite file at `.ai/memex.db` containing both Memex v1's wiki content and Atelier's own tables. Atelier's [CLAUDE.md](../../CLAUDE.md) still enforces "Memex must be set up before any Atelier command will work" against that v1 model.

Memex v2 shipped on 2026-05-16 with a fundamentally different shape: it is now a **machine-global** personal knowledge runtime with a federated Index, an Archivist for raw bodies, a Librarian subagent for classification, and a multi-store substrate that explicitly supports **consumer-supplied SQL migrations** for arbitrary stores (Atelier being the canonical example named in the v2 spec).

Memex v2 §12 ("Out-of-scope") names "Atelier retrofit to write through Memex Librarian + Core" as the natural next phase. This document specifies that retrofit, plus a dual-mode fallback so Atelier remains useful for users who have not (yet) installed Memex.

## 2. Goals

1. **First-class Memex consumer.** When Memex v2 is installed, Atelier writes through `memex:index:write` (for documents) and `memex:core:*` (for state), shares Memex's machine-global `~/.memex/agents.db`, and lives as a registered Memex Core store at `~/.memex/atelier.db`.
2. **Safety net.** When Memex is not installed, Atelier still works via a slim project-local SQLite backend with FTS5-only retrieval. No federated Index, no vector retrieval, no Librarian dispatch — but full workflow capability.
3. **Auto-detected mode.** Mode selection is automatic at every operation. Users do not configure it.
4. **Self-installing into Memex.** On first run in Memex mode, Atelier seeds its roles and shipped agent profiles into `~/.memex/agents.db`, creates its store via `memex:core:create-store` with its own migrations, and records bootstrap state. Idempotent — safe to re-run.
5. **Frictionless migration.** When Memex becomes available after Atelier has been running locally, prompt the user once; on consent, replay every local row through the appropriate Memex write path and retire the local file.
6. **No surface bloat.** The 4 user-facing skills (load, save, ingest, execute) stay the only Claude-Code-visible Atelier surface. All new procedures (bootstrap, migration, local-mode WIKI, dispatch) live under `internal/` and are reached via routing — same pattern Memex v2 itself uses with `memex:run`.

## 3. Non-goals

- Multi-machine sync of Atelier state.
- Multi-tenant (multiple humans on one Atelier install).
- Atelier-specific embedding provider configuration (Memex mode inherits Memex's provider choice; Local mode has no embeddings by definition).
- Cross-project task or session reporting in Local mode (per-project files cannot be queried as one set; Memex mode gives this for free via the federated Index).
- Auto-migration **back** from Memex to Local (one-way only).
- Migrating data from the legacy Memex-v1 era `.ai/memex.db` files. Those are clean-slate; users can manually re-ingest documents.

## 4. Architecture

### 4.1 Dual-mode persistence

```
                         ┌────────────────── Atelier user-facing skills (UNCHANGED) ──────────────────┐
                         │       /atelier:load    /atelier:save    /atelier:ingest    /atelier:execute  │
                         └────────────────────────────────┬──────────────────────────────────────────────┘
                                                          │
                                                          ▼
                                       ┌──────────────────────────────────────┐
                                       │  scripts/backend.py  (persistence    │
                                       │  facade with mode dispatch)          │
                                       └──────────┬───────────────────────────┘
                                                  │  detect_mode()
                              ┌───────────────────┴────────────────────┐
                              ▼                                        ▼
                ┌─────────── MEMEX MODE ───────────┐      ┌─────────── LOCAL MODE ───────────┐
                │                                  │      │                                  │
                │  memex:index:write   (docs+tasks)│      │  internal/local/wiki-write       │
                │  memex:core:insert/update/delete │      │  internal/local/wiki-search      │
                │  memex:core:query                │      │  internal/local/wiki-archive     │
                │  memex:core:register-role        │      │  internal/local/state-crud       │
                │  memex:core:register-agent       │      │                                  │
                │  memex:core:create-store         │      │  SQLite at <project>/.ai/        │
                │                                  │      │    atelier.db (FTS5 only,        │
                │  Backed by:                      │      │    no embeddings, no Librarian)  │
                │    ~/.memex/agents.db            │      │                                  │
                │    ~/.memex/index.db             │      │                                  │
                │    ~/.memex/atelier.db           │      │                                  │
                │    ~/.memex/raw/                 │      │                                  │
                └──────────────────────────────────┘      └──────────────────────────────────┘
```

### 4.2 Mode detection

`scripts/backend.py:detect_mode()` returns `"memex"` if **both** are true:

1. `~/.memex/registry.json` exists (Memex v2 is bootstrapped on this machine).
2. The Memex plugin is reachable via Claude Code's plugin cache (`~/.claude/plugins/cache/agora/memex/<v>/.claude-plugin/plugin.json` parseable).

Otherwise returns `"local"`. Result is cached for the lifetime of the Python process (each Atelier command invocation re-detects fresh).

### 4.3 Persistence facade

`scripts/backend.py` exposes a uniform API used by all of Atelier's business logic:

```python
# Document-shaped writes (go through Librarian in Memex mode;
# FTS5-indexed in Local mode)
backend.write_document(domain, title, body, metadata, caller_agent_id) -> dict
backend.write_task(title, description, ...) -> dict  # Tier 2 — caller-built librarian_output per §6.2
backend.write_meeting(...) -> dict

# Operational state (direct CRUD in both modes)
backend.upsert_session(...) -> dict
backend.transition_phase(...) -> dict
backend.update_task_status(task_id, status) -> dict
backend.record_phase_bypass(...) -> dict

# Reads
backend.find_documents(query, filters) -> list[dict]
backend.get_task(task_id) -> dict
backend.list_tasks(project_id, status) -> list[dict]
```

In Memex mode each method dispatches to `memex:run` (which routes to `memex:index:write` or `memex:core:*`). In Local mode each method calls the equivalent `internal/local/*` procedure (which directly writes the project-local SQLite).

Atelier's existing `scripts/{projects,tasks,documents,meetings,sessions,workflow}.py` modules are rewritten to call `backend.*` instead of opening SQLite connections directly. `scripts/db.py` is **deleted**.

## 5. Memex mode — bootstrap

On every Atelier command in Memex mode, before any other work, run the bootstrap check:

1. Read `~/.memex/atelier.bootstrap.json` (if exists). If present and `version` matches the installed Atelier version, **skip** bootstrap.
2. Otherwise, run `internal/bootstrap-memex/SKILL.md`:
   1. **Roles** — for each row in `templates/roles.json` (PM, Software Architect, Software Engineer, Tech Writer, QA, Designer, …), call `memex:core:register-role`. Idempotent — Memex Core's register-role no-ops on name collision and returns the existing row.
   2. **Agents** — for each row in `templates/agents/*.json` (Atelier's shipped agent personas), call `memex:core:register-agent`. Idempotent on `agent_id`.
   3. **Store** — call `memex:core:create-store` with:
      - `store_name`: `"atelier"`
      - `migrations_dir`: `<atelier-plugin-root>/migrations/`
      - This provisions `~/.memex/atelier.db` (if missing) and replays any new migrations. Memex Core tracks applied migrations in the store's own `migrations` table, so this is idempotent.
   4. **Write marker** — `~/.memex/atelier.bootstrap.json` = `{"version": <atelier_version>, "bootstrapped_at": <iso8601>}`. Future runs skip.

Bootstrap is fast in steady state (one file read, version compare) and self-healing — if a user reinstalls Memex from scratch, the next Atelier command will repopulate.

## 6. Memex mode — write paths (three-tier model)

**Prerequisite:** This retrofit targets **Memex v2.2.0 or later** — the version where `memex:index:write` first accepts an optional caller-built `librarian_output` and ships `librarian.validate_output()` as the shared schema check. Memex's [docs/specs/2026-05-16-memex-v2-redesign-design.md §6.3](../../../memex/docs/specs/2026-05-16-memex-v2-redesign-design.md) documents the dual-mode contract.

Atelier writes fall into three tiers based on what kind of mutation they represent. Each tier has a distinct Memex surface and cost profile.

### 6.1 Tier 1 — Pure-state mutation (direct Memex Core, no Index touch)

Rows where mutation does **not** change the Index's `searchable` text and which never need free-text search:

| Atelier operation | Atelier table | Memex surface |
|---|---|---|
| `update_task_status` | `tasks` | `memex:core:update` |
| `transition_phase` | `projects.phase` column | `memex:core:update` |
| `record_phase_bypass` | `phase_bypasses` | `memex:core:insert` |
| `upsert_session` | `sessions` | `memex:core:insert / update` |
| `add_participant` | `meeting_participants` | `memex:core:insert` |
| `assign_task` | `tasks.assigned_to` column | `memex:core:update` |

These rows either have no `index_id` column (`sessions`, `phases`, etc.) or have one but the mutation doesn't affect what's already in the Index (e.g., a status flip doesn't change the task's title or description, so the FTS5 row is still correct).

**Cost:** one SQL UPDATE/INSERT. No LLM call. No Index touch.

### 6.2 Tier 2 — Structured-row create / content edit (caller-built `librarian_output`)

Rows where Atelier creates a new document or edits its content; Atelier knows the domain and can build a deterministic classification:

| Atelier operation | Atelier table | Domain |
|---|---|---|
| `create_project`, `update_project` (name/description) | `projects` | `project` |
| `create_task`, content edits to `description`/`notes` | `tasks` | `task` |
| `create_document`, `update_document` (filename/title) | `project_documents` | `project_doc` |
| `create_meeting` | `meeting_minutes` | `meeting` |

Each write goes through `memex:index:write` with a **caller-built `librarian_output`** (Memex v2.2.0+ contract):

```python
from scripts.agents import librarian as memex_librarian   # via plugin cache
from scripts import embeddings as memex_embeddings

output = memex_librarian.validate_output({
    "index_id":   _new_uuid7(),
    "key":        _slug(title),
    "domain":     "task",                                  # Atelier's fixed vocabulary
    "searchable": f"{title}. {body[:1500]}",
    "metadata":   {"project_id": project_id, "priority": priority},
    "relations":  [{"to_index_id": project_index_id, "rel_type": "part_of"}],
})
try:
    embedding = memex_embeddings.encode(output["searchable"])
except Exception:
    embedding = None

memex_librarian.write_entry(
    payload=row_to_insert,
    librarian_output=output,
    target_store="atelier",
    target_table="tasks",
    caller_agent_id="atelier-pm-1",
    embedding=embedding,
)
```

**Cost:** one SQL INSERT + one Index INSERT + one embedding call. **No LLM dispatch.** Synchronous Python end-to-end.

`relations` is caller-built — for structured graph edges (`task part_of project`, `meeting produced decision`) this is *strictly more accurate* than what the Librarian subagent would extract from prose.

### 6.3 Tier 3 — Prose ingest (full Librarian subagent dispatch)

Atelier's `/atelier:ingest` skill, when given an external article, transcript, or research dump where the domain and relations must be extracted from the text. This is the only Atelier path that pays the LLM cost.

Flow uses Memex's Option-B Task-tool dispatch pattern (the same one Memex Brain uses for its `ingest` op):

1. Python `ingest_prepare(...)` builds the Librarian's prompt via `librarian.build_prompt(payload, target_store="atelier", caller_agent_id=...)`.
2. Atelier's `/atelier:ingest` SKILL.md dispatches the Task tool with that prompt.
3. Subagent returns JSON classification.
4. Python `librarian.parse_response(...)` validates.
5. Python `librarian.write_entry(...)` persists.

**Cost:** one LLM call per ingest. Justified — only invoked when domain/relations genuinely need extraction.

### 6.4 Atelier domain vocabulary (Tier 2 invariant)

Atelier owns the `domain` field for its rows. Memex doesn't enforce an enum — the contract is "use a small, stable, documented set". v1 vocabulary:

| Domain | Used for |
|---|---|
| `project` | `projects` table rows |
| `task` | `tasks` table rows |
| `meeting` | `meeting_minutes` table rows |
| `project_doc` | `project_documents` table rows |
| `adr` | Architecture Decision Records — future, subset of `project_doc` |

This vocabulary is documented in `internal/memex/domain-vocabulary.md` (shipped by Plan 1 Task 5) and validated as a constant in `scripts/backend_memex.py`. Adding a new domain is a deliberate spec-revision step, not an inline decision.

### 6.5 Roles and agents (Memex mode)

In Memex mode, Atelier's `~/.memex/atelier.db` has **no** `roles` or `agents` tables — those live in `~/.memex/agents.db` (Memex's machine-global agents store) and are shared with every Memex consumer. Atelier's bootstrap (§5) seeds Atelier-specific roles and shipped agent profiles into that store via `memex:core:register-role` / `register-agent`. Foreign-key columns like `tasks.assigned_to` and `meetings.created_by` continue to hold an agent_id string; resolution goes through `memex:core:get-agent`.

Local mode is different — see §7 for how Local mode handles roles/agents in the project-local DB.

### 6.6 Re-interpretation of the "always through Librarian" brainstorm decision

The brainstorm picked "always through Librarian" for tasks. With the Memex v2.2.0 contract, that wish is more usefully restated as **"always indexed in Memex, via `librarian.write_entry`"**. The Librarian LLM dispatch is a separate axis. Atelier honors the spirit of the decision (every content write lands in the federated Index) without paying the LLM cost on every status flip. Tier 1 covers status-only mutations; Tier 2 covers content writes; Tier 3 is reserved for prose where the LLM earns its keep.

The earlier "deferred `lite_update`" mitigation is no longer needed — Tier 1 IS that mitigation, shipped in v1.

## 7. Local mode — slim backend

When Memex is absent, Atelier falls back to a project-local SQLite at `<project-root>/.ai/atelier.db`. This is a **slim Memex** — not a parallel architecture:

| Feature | Memex mode | Local mode |
|---|---|---|
| Document index | `~/.memex/index.db` federated | Local `documents` FTS5 table |
| Vector retrieval | Yes (Memex hybrid) | **No** (FTS5 only) |
| Raw archive | Archivist (`~/.memex/raw/`) | Local `<project>/.ai/raw/` directory |
| Librarian classification | Yes (LLM dispatch) | **No** — Python-computed `key`, `domain` fixed by table |
| Cross-project search | Yes (one Index) | **No** (per-project files) |
| Embeddings | Provider-configurable | `embedding=NULL` always |
| Agents/roles store | `~/.memex/agents.db` (shared) | Local `roles`/`agents` tables |
| State tables | `~/.memex/atelier.db.*` | Local equivalents in same file |

Local-mode procedures live under `internal/local/`:

- `internal/local/wiki-write/SKILL.md` — insert document row + FTS5 + raw file copy
- `internal/local/wiki-search/SKILL.md` — FTS5 query over the local documents table
- `internal/local/wiki-archive/SKILL.md` — copy raw body into `.ai/raw/`
- `internal/local/state-crud/SKILL.md` — generic insert/update/delete/query helpers

**These procedures are not surfaced as Claude Code skills.** They are plain markdown files under `internal/` — Atelier's user-facing skills route to them by reading the file and following its instructions (the established Atelier pattern).

## 8. Migration — Local → Memex (one-shot, on detection)

Trigger: at the start of any Atelier command, if `detect_mode() == "memex"` AND a project-local `.ai/atelier.db` exists AND no `.ai/atelier.migrated` marker present AND no `.ai/atelier.local-only` opt-out marker present.

Behavior:

1. Run `internal/migrate-local-to-memex/SKILL.md`:
   - Open the local DB, count rows per table, present a summary to the user.
   - Prompt: `Memex detected. Migrate this project's Atelier data to Memex? [y/N]`.
   - **If y:**
     - For each document-shaped row (projects, project_documents, meeting_minutes, tasks): replay through `memex:index:write` (Librarian).
     - For each operational row (sessions, phases, phase_bypasses, etc.): replay through `memex:core:insert`.
     - Roles + agents are merged via `memex:core:register-role / register-agent` (idempotent).
     - Rename `.ai/atelier.db` → `.ai/atelier-pre-migration-<iso8601>.db`. Keep on disk for safety; user can delete later.
     - Write `.ai/atelier.migrated` marker with timestamp + migrated_row_count.
   - **If N:**
     - Write `.ai/atelier.local-only` marker. Atelier stays in Local mode for this project even though Memex is available.
     - User can re-trigger by deleting the marker.

Migration is per-project, not global. A user can migrate some projects and leave others local.

Migration is **non-destructive on failure**. If any step fails, the migrated file is left as-is, no marker is written, and the next command retries. We do not write partial state to Memex without completing.

## 9. Skill surface

Visible to Claude Code (unchanged from today):

- `/atelier:load` — load context for a project
- `/atelier:save` — persist session state
- `/atelier:ingest` — pull external doc into Atelier-managed Brain
- `/atelier:execute` — phase/workflow entry point

Internal (not surfaced; reached only by reading via routing from the four above):

| New under `internal/` | Purpose |
|---|---|
| `internal/bootstrap-memex/SKILL.md` | One-time Memex-mode seed (roles, agents, store) |
| `internal/migrate-local-to-memex/SKILL.md` | One-shot per-project local→Memex replay |
| `internal/detect-mode/SKILL.md` | Mode-detection contract + caching |
| `internal/local/wiki-write/SKILL.md` | Slim FTS5 document insert (Local) |
| `internal/local/wiki-search/SKILL.md` | FTS5 search (Local) |
| `internal/local/wiki-archive/SKILL.md` | Raw-body archive (Local) |
| `internal/local/state-crud/SKILL.md` | Generic state CRUD (Local) |
| `internal/memex/dispatch-write/SKILL.md` | Wraps `memex:index:write` calls with Atelier defaults |
| `internal/memex/dispatch-core/SKILL.md` | Wraps `memex:core:*` calls with Atelier defaults |

All 13 existing `internal/dev-*/SKILL.md` procedures stay but get rewritten to call `backend.*` instead of opening SQLite directly.

## 10. Project scoping

In Memex mode, every Atelier row lives in one machine-global `~/.memex/atelier.db`. To know which project a command applies to, Atelier auto-detects from CWD:

```python
def current_project_id() -> int | None:
    cwd = Path.cwd()
    git_root = find_git_root(cwd)              # walk up to .git/
    if git_root is None:
        return None  # not in a repo
    repo_url = git_remote_url(git_root) or str(git_root.resolve())
    project_key = hash_project_key(repo_url)    # stable per-repo
    return backend.find_project_by_key(project_key)  # may return None → prompt to register
```

If no project is registered for this repo and the command requires one, prompt: `No Atelier project registered for this repo. Register now? [y/N]`. On yes, run `internal/dev-create-project/SKILL.md`.

Same mechanism in Local mode — just dispatches to local-table queries instead of Memex Core.

## 11. Schema

Atelier ships its existing 5 SQL migration files (`migrations/001_initial_schema.sql` through `005_soft_walls.sql`) **with two changes**:

1. **Drop the `roles` and `agents` CREATE TABLE statements from `001_initial_schema.sql`.** A new `006_drop_atelier_agents.sql` adds the DROP for users upgrading from an earlier Atelier (Local-mode users only — Memex-mode users never had these in their atelier.db because the migration was applied via Memex Core, which is run only on bootstrap with the post-drop schema).
   - Local mode also drops its `roles`/`agents` and seeds Memex-compatible role/agent rows into a small `agents_shim` view? Actually simpler: Local mode keeps `roles` + `agents` (it has no `~/.memex/agents.db` to defer to). The DROP applies only in Memex mode. We handle this with a Memex-mode-only migration runner that filters out the agents/roles DDL.
   - Implementation detail to resolve during plan-writing: cleanest is two migration directories — `migrations/shared/` (everything except roles/agents) + `migrations/local-only/` (the roles/agents CREATE statements). Memex bootstrap supplies only `shared/`. Local bootstrap supplies both.

2. **Add `index_id TEXT` column to** `projects`, `project_documents`, `meeting_minutes`, `tasks` — populated in Memex mode by the Librarian; nullable + ignored in Local mode.

## 12. Testing strategy

Wave-based per the user's intent for the implementation plan; this section documents what the test surface must cover.

| Area | Test type |
|---|---|
| Mode detection | Unit tests on `detect_mode()` with mocked filesystem |
| Bootstrap idempotency | Re-run bootstrap on populated agents.db — assert no duplicates, marker correctness |
| Memex-mode writes | Integration test against a temp `~/.memex/` — assert `~/.memex/atelier.db` rows + `index.db.documents` rows present after document write |
| Local-mode writes | Same surface, asserts the project-local file instead |
| Mode-switch migration | Seed a local DB, install Memex (in-test mock), assert migration replays every row and leaves a marker |
| Migration declined | Seed local DB, decline migration, assert `.local-only` marker, assert subsequent commands still work in Local mode |
| Crash mid-migration | Inject failure on row 3 of 10 — assert no marker, assert local DB intact, assert retry succeeds |
| Surface invariants | Assert exactly 4 user-facing skills are registered in `.claude-plugin/plugin.json` |
| Internal-only invariant | Assert all `internal/local/*` and `internal/memex/*` procedures have no `name:` field that would expose them as slash commands |

## 13. Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | Atelier accidentally falls into Tier 3 (LLM dispatch) for routine writes | The tier mapping in §6.1–§6.3 is enforced in `backend_memex.py` — `write_task` / `write_meeting` / `write_document` / `write_project` ALWAYS take the Tier 2 path. Tier 3 is reachable only through Atelier's `/atelier:ingest` skill with `--external-prose` semantics. Unit test asserts no Tier 2 write reaches `librarian.build_prompt`. |
| 2 | Bootstrap fails mid-step (e.g., register-role succeeds, create-store fails) | Each step is idempotent; no marker is written until all steps complete. Next run retries. |
| 3 | Local mode + Memex mode drift in schemas over time | Single `migrations/shared/` directory is the source of truth for both backends. CI test asserts schema parity. |
| 4 | A user with both a local DB and a Memex install runs many projects from different working directories without migrating | Per-project markers solve this: each project's migration choice is recorded in `.ai/atelier.migrated` or `.ai/atelier.local-only`. Atelier prompts once per project. |
| 5 | Two backends to maintain forever | Acknowledged. Every new feature must work in both modes (or be explicitly Memex-only). The facade in `scripts/backend.py` is the single seam; new methods are added there with two impls. |
| 6 | Memex Core's `register-role` / `register-agent` must be safe to call against an already-seeded entry | Memex Core's spec advertises idempotency. The plan-writer must add an integration test that re-runs bootstrap and asserts no duplicate rows + no errors. If a non-idempotent surface is found, raise upstream against Memex before merging this retrofit. |
| 7 | Atelier-domain values (project/document/meeting/task) are not in Memex Librarian's known taxonomy | The Librarian's system prompt is read from `agents.db.agents.profile`. Bootstrap can include an "Atelier taxonomy supplement" that gets concatenated into the Librarian's prompt when an Atelier-tagged document is dispatched. Implementation deferred to plan-writing. |

## 14. Out of scope (do not implement without spec revision)

- Multi-machine sync of either backend.
- Multi-tenant Atelier (multiple humans on one install).
- Auto-migration from Memex back to Local.
- Per-project Atelier stores registered against the global Memex registry (rejected in brainstorm — machine-global was chosen).
- Embedding provider configuration distinct from Memex's.
- Migration from the legacy v1-era `.ai/memex.db` files (separate concern; clean-slate).
- A `lite_update` task write path that bypasses Memex Index entirely on content edits — Tier 1 (§6.1) already handles status-only mutations and content edits use Tier 2 (no LLM cost). No further mitigation needed in v1.

## 15. Open implementation decisions deferred to `writing-plans`

These do not block design approval but are decisions the plan-writer will need to make:

1. Single `migrations/` directory with conditional filtering vs. two directories (`migrations/shared/` + `migrations/local-only/`). §11 leans toward two.
2. Where Atelier's shipped agent personas live as JSON in this repo (`templates/agents/*.json` vs. `internal/seed/agents.py`).
3. Exact dispatch wrapper API in `scripts/backend.py` — pure dict-in/dict-out vs. typed dataclasses.
4. How `internal/memex/dispatch-write/SKILL.md` is invoked from Python — via the Task tool from the calling skill's wrapper, or via a Python-level subprocess that re-enters the agent runtime. Probably the former (matches Memex's own Option-B pattern).
5. Failure semantics when the Librarian subagent returns malformed JSON for an Atelier-domain doc — retry once, then fall back to a deterministic Python classification? Or hard-fail like Memex does today?
6. Migration prompt UX — single y/N at the top of the next command vs. a dedicated `atelier:migrate` skill the user invokes explicitly. (Spec leans toward the y/N prompt for frictionless behavior.)

## 16. Wave structure (preview for `writing-plans`)

The implementation plan should be wave-based — independent tasks dispatched in parallel within a wave, sequential between waves. Sketch:

```
Wave 0 — Foundations                                       [all parallel]
  - Persistence facade signature (scripts/backend.py skeleton)
  - Mode-detection module
  - Atelier role + agent seed JSON in templates/
  - migrations/ split: shared/ + local-only/

Wave 1 — Memex-mode write paths                            [parallel; depends W0]
  - internal/memex/dispatch-write
  - internal/memex/dispatch-core
  - internal/bootstrap-memex
  - Backend.write_document / write_task / write_meeting (Memex impl)
  - Backend.upsert_session / transition_phase / record_phase_bypass (Memex impl)

Wave 1' — Local-mode write paths                           [parallel; depends W0]
  - internal/local/wiki-write
  - internal/local/wiki-search
  - internal/local/wiki-archive
  - internal/local/state-crud
  - Backend.* (Local impl, mirrors W1 signatures)

Wave 2 — Business-logic rewrites                           [parallel; depends W1+W1']
  - Rewrite scripts/projects.py to use backend.*
  - Rewrite scripts/tasks.py
  - Rewrite scripts/documents.py
  - Rewrite scripts/meetings.py
  - Rewrite scripts/sessions.py
  - Rewrite scripts/workflow.py
  - Delete scripts/db.py
  - Update scripts/migrate.py (or retire it; bootstrap subsumes it in Memex mode)

Wave 3 — Migration                                         [serial; depends W2]
  - internal/migrate-local-to-memex
  - Per-project markers (.ai/atelier.migrated, .ai/atelier.local-only)
  - User-prompt UX in the entry skills
  - Crash-safety tests

Wave 4 — Surface + docs                                    [parallel; depends W2]
  - Update .claude-plugin/plugin.json (verify only 4 surfaced skills)
  - Update CLAUDE.md (drop v1 dependency check, document dual-mode)
  - Update README.md
  - CHANGELOG.md entry
  - Bump version (Atelier 1.0.13 → 1.1.0 — new feature, backward-compatible: Local mode preserves today's behavior)

Wave P — Packaging + release                               [last]
  - Run full test suite
  - Build dist bundle (if Atelier has one)
  - Tag + push
  - Update agora marketplace pin to new Atelier version
```

The plan-writer will refine these and produce per-wave acceptance criteria.
