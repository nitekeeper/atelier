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
# Document-shaped writes — Tier 2 (caller-built librarian_output, no LLM)
backend.write_project(*, workspace_id, slug, name, description,
                      created_by) -> dict
backend.write_document(*, workspace_id, project_id, domain, subdomain,
                       title, body, metadata, caller_agent_id,
                       source_url=None,
                       relations: list[dict] = ()) -> dict
backend.write_task(*, workspace_id, project_id, title, description,
                   subdomain, created_by, assigned_to=None,
                   priority=0, notes=None,
                   relations: list[dict] = ()) -> dict
backend.write_meeting(*, workspace_id, project_id, title, date,
                      summary, decisions, subdomain, created_by,
                      relations: list[dict] = ()) -> dict

# Operational state — Tier 1 (direct CRUD, no Index touch)
backend.upsert_session(*, project_id, agent_id, ...) -> dict
backend.transition_phase(*, project_id, to_phase, agent_id, ...) -> dict
backend.update_task_status(*, task_id, status, notes=None) -> dict
backend.record_phase_bypass(*, project_id, from_phase, to_phase,
                            reason, agent_id) -> dict

# Workspace + project resolution (used by scripts/scope.py)
backend.find_or_create_workspace(*, identity, slug, name) -> dict
backend.find_workspace_by_identity(*, identity) -> dict | None
backend.list_workspaces() -> list[dict]
backend.find_project(*, workspace_id, slug) -> dict | None
backend.list_projects(*, workspace_id) -> list[dict]

# Reads
backend.find_documents(*, query, workspace_id=None, project_id=None,
                       domain=None, subdomain=None, limit=10) -> list[dict]
backend.get_task(*, task_id) -> dict | None
backend.list_tasks(*, project_id, status=None) -> list[dict]
backend.get_document(*, doc_id) -> dict | None
```

In Memex mode each method dispatches to `memex:run` (which routes to `memex:index:write` or `memex:core:*`). In Local mode each method calls the equivalent `internal/local/*` procedure (which directly writes the project-local SQLite).

Atelier's existing `scripts/{projects,tasks,documents,meetings,sessions,workflow}.py` modules are rewritten to call `backend.*` instead of opening SQLite connections directly. New modules `scripts/workspaces.py` and `scripts/scope.py` are added for the workspace + project resolution layer (§10). `scripts/db.py` is **deleted**.

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

### 6.4 Two-level taxonomy: domain + subdomain

Atelier classifies every row with **two** orthogonal fields:

- **`domain`** — written to `~/.memex/index.db.documents.domain`. The cross-plugin, machine-global filter Memex Brain queries against. Small, stable, spec-amendment-only to extend. Atelier validates against `scripts.domain_vocabulary.DOMAINS` before every Tier 2 write.
- **`subdomain`** — an Atelier-internal column on each respective Atelier table (`tasks.subdomain`, `meeting_minutes.subdomain`, `project_documents.subdomain`, etc.). Free-form within Atelier's lightweight policy (controlled vocabulary per domain). Never written to Memex Index — stays in Atelier SQL.

The split lets `memex:brain:ask "show me all my standups"` work cross-plugin via the `domain="meeting"` filter, then narrow inside Atelier with `subdomain="standup"`. Subdomain proliferation doesn't pollute Memex's namespace.

#### Domain vocabulary (cross-plugin, stable)

| Domain | Used for | Atelier source table | Promotion rationale |
|---|---|---|---|
| `project` | Top-level work efforts | `projects` | Always cross-project ("what projects have I run") |
| `task` | Atomic work items | `tasks` | Cross-project recall ("what bugs did I fix last quarter") |
| `meeting` | Meeting minutes | `meeting_minutes` | Decisions/discussions cross-cut projects |
| `design` | System / feature designs | `project_documents` (subset) | Patterns recur across projects — "every auth design I've drafted" |
| `adr` | Architecture Decision Records | `project_documents` (subset) | High-value cross-project lookup is the canonical ADR use case |
| `research` | Reference / evaluation notes | `project_documents` (subset) | Tech-topic recall ("notes on Postgres tuning across all projects") |
| `postmortem` | Incident / release / retro write-ups | `project_documents` (subset) | Lessons cross-cut by failure mode, not project |
| `log` | Daily/decision/lesson journals | `project_documents` (subset, or workspace-level) | Time-bounded recall; often workspace- or human-scoped, not project-scoped |
| `project_doc` | Catch-all for typed-but-not-promoted docs (e.g., `plan`) | `project_documents` (catch-all) | Generic bucket; specific subdomain in the Atelier column |

`plan` deliberately does **not** get its own domain. Implementation plans are project-bound and rarely useful cross-project; they ride under `project_doc` with `subdomain="plan"`.

#### Subdomain vocabulary (Atelier-internal, lightweight extension)

| Domain | Stable subdomains |
|---|---|
| `task` | `bug`, `feature`, `chore`, `spike`, `refactor` |
| `meeting` | `standup`, `design-review`, `retro`, `1-1`, `customer`, `incident`, `kickoff`, `planning` |
| `design` | `api`, `data`, `infra`, `ux`, `security`, `migration` |
| `research` | `evaluation`, `reference`, `summary`, `comparison` |
| `postmortem` | `incident`, `release`, `retro` |
| `log` | `daily`, `decision`, `lesson` |
| `project_doc` | `plan`, `runbook`, `release-notes`, `pr-description`, free-form |
| `project`, `adr` | (no subdomains — atomic) |

Subdomain enforcement is **soft** — unknown values are accepted (no `assert_valid`), but `scripts.domain_vocabulary.SUBDOMAINS` documents the canonical set per domain. Drift is acceptable here; a future audit can roll up frequencies and promote stable additions into the controlled list.

#### Addition policy

- **Adding a domain** — spec amendment; updates `DOMAINS` frozenset; requires test coverage.
- **Adding a subdomain** — Atelier internal; update `SUBDOMAINS[domain]` list in `scripts/domain_vocabulary.py`. No spec change required, but PR comment should justify it.

This vocabulary is documented in `internal/memex/domain-vocabulary.md` and validated as constants in `scripts/domain_vocabulary.py` (both shipped by Plan 1 Task 6).

### 6.5 Roles and agents (Memex mode)

In Memex mode, Atelier's `~/.memex/atelier.db` has **no** `roles` or `agents` tables — those live in `~/.memex/agents.db` (Memex's machine-global agents store) and are shared with every Memex consumer. Atelier's bootstrap (§5) seeds Atelier-specific roles and shipped agent profiles into that store via `memex:core:register-role` / `register-agent`. Foreign-key columns like `tasks.assigned_to` and `meetings.created_by` continue to hold an agent_id string; resolution goes through `memex:core:get-agent`.

Local mode is different — see §7 for how Local mode handles roles/agents in the project-local DB.

### 6.6 Re-interpretation of the "always through Librarian" brainstorm decision

The brainstorm picked "always through Librarian" for tasks. With the Memex v2.2.0 contract, that wish is more usefully restated as **"always indexed in Memex, via `librarian.write_entry`"**. The Librarian LLM dispatch is a separate axis. Atelier honors the spirit of the decision (every content write lands in the federated Index) without paying the LLM cost on every status flip. Tier 1 covers status-only mutations; Tier 2 covers content writes; Tier 3 is reserved for prose where the LLM earns its keep.

The earlier "deferred `lite_update`" mitigation is no longer needed — Tier 1 IS that mitigation, shipped in v1.

### 6.7 Key construction

The `key` field in `index.db.documents` is a stable, human-readable identifier used in search-result rendering, citations, and URL generation. Naive `slug(title)[:64]` collides catastrophically once users write multiple same-title docs per day (daily standups, status updates, iterative design drafts).

Atelier composes keys with enough context to be both unique and scannable:

```
key = "<workspace_slug>/<project_slug>/<domain>/<date>-<title_slug>-<seq>"
```

Components:

| Part | Source | Default | Notes |
|---|---|---|---|
| `workspace_slug` | `workspaces.slug` | required | One workspace = one repo |
| `project_slug` | `projects.slug` | `no-project` | Literal placeholder for workspace-scoped docs (e.g., a daily `log` not bound to a project) |
| `domain` | `librarian_output.domain` | required | One of `DOMAINS` |
| `date` | `created_at.date()` ISO `YYYY-MM-DD` | required | UTC date of creation |
| `title_slug` | `slug(title)[:48]` | required | Lower-snake (`-`), max 48 chars |
| `seq` | smallest unused integer ≥ 1 for the same (`workspace/project/domain/date/title`) prefix | required | Disambiguates same-title-same-day; written explicitly so the key is deterministic |

Examples:

```
auth-service/oauth-rewrite/design/2026-05-16-token-storage-1
billing/q2-cleanup/adr/2026-05-16-postgres-only-1
auth-service/(no-project)/meeting/2026-05-16-standup-3        ← third standup that day
auth-service/oauth-rewrite/task/2026-05-16-fix-cookie-bug-1
internal-dashboard/(no-project)/log/2026-05-16-daily-1
```

#### Sequence allocation

On every Tier 2 write, Atelier queries the Index for existing keys matching the prefix-up-to-seq and assigns `seq = max(existing_seq) + 1` (or `1` if none exist). One read per write. Race-free under single-process Atelier (the only writer for this store); document the assumption.

#### Length and characters

- All slug components: lower-case ASCII, `[a-z0-9-]+`, leading/trailing dashes stripped, repeated dashes collapsed.
- No length cap on the assembled `key`; typical 60–120 chars. Memex Index `key` column is TEXT — unbounded.
- `(no-project)` is the literal string used when `project_slug` is unset (workspace-level documents).

#### Why not just rely on `index_id`?

`index_id` is a UUID — globally unique but unreadable. `key` is the user-facing handle. Both are needed: `index_id` for cross-document references, `key` for human display and stable URLs.

### 6.8 Searchable text

`librarian_output.searchable` is what Memex's Index FTS5 indexes. If a substring isn't in `searchable`, no `memex:brain:ask` query for that substring will return the document.

The Tier 2 implementation rule: **`searchable` MUST contain every word a future query is likely to use, including body content.**

#### Composition

```python
searchable = "\n\n".join(filter(None, [
    title,
    body or "",                       # full body — NO truncation
    metadata_narrative_excerpt(metadata),  # join string-valued metadata fields
]))
```

- **Full body** — no character cap. FTS5 handles arbitrarily long text. The 1500-char cap I'd initially proposed was wrong; it would hide the middle pages of any nontrivial design doc.
- **Narrative metadata** — for rows that carry searchable structured fields (e.g., `tasks.notes`, `meeting_minutes.decisions`, `projects.description`), include them in `searchable`.

#### Special case: `project_documents` references files on disk

For project documents (`domain ∈ {design, adr, research, postmortem, log, project_doc}`), the row is a **pointer to a file** at `<workspace_root>/<filename>`. The body Atelier passes to `write_document` must be the **actual file content**, not a placeholder:

```python
# scripts/documents.py:create_document
file_path = workspace_root() / filename
body = file_path.read_text(encoding="utf-8")  # full file content
result = backend.write_document(
    domain=resolved_domain, title=title, body=body, ...
)
```

If the file does not yet exist on disk (rare — usually the user authors the file first, then registers it with Atelier), `create_document` raises `FileNotFoundError` rather than silently indexing a placeholder body. Indexing an empty/placeholder body makes the document undiscoverable, which is worse than a hard error at registration time.

#### Searchable updates on content edits

Editing a document's title or body must re-emit `searchable` to keep FTS5 in sync. The Tier 2 contract treats every content edit as a fresh `librarian.write_entry` call (new `index_id` row) — Memex's design accepts this; the prior `index_id` row remains for citation stability, and the new row supersedes via `relations` (`rel_type="supersedes"`). Plan 2 Task 1 must document this; Plan 3 Task 1's `update_document` rewires to issue a fresh Tier 2 write when title/body changes, not an in-place UPDATE on the documents row.

### 6.9 Relations populated by Atelier

The Memex Index `relations` table is the primary cross-document graph. The Memex brief explicitly calls out that **caller-built relations are strictly more accurate than what the Librarian subagent would extract from prose** for structured writers — so Atelier should populate them aggressively for known edges.

#### Edges Atelier emits automatically on Tier 2 writes

| Edge | Created by | rel_type |
|---|---|---|
| `task → project` | `write_task` (when `project_id` provided) | `part_of` |
| `meeting → project` | `write_meeting` (when `project_id` provided) | `part_of` |
| `project_documents → project` | `write_document` (always — every project doc belongs to a project) | `part_of` |
| `project → workspace` (only if Memex `relations` supports cross-domain edges to non-Atelier rows; otherwise omit) | `write_project` | `part_of` |
| new document → old document of same `key` prefix | content edit via `update_document` | `supersedes` |

#### Edges Atelier emits when caller supplies them

| Edge | Source | rel_type |
|---|---|---|
| `design → adr` | caller passes `derives_from_design_id` when creating ADR | `derives_from` |
| `meeting → adr` / `meeting → decision` | caller passes `decided_ids` when creating meeting minutes | `decided` |
| `task → adr` (task implements a decision) | caller passes `implements_id` | `implements` |
| `postmortem → incident_meeting` | caller passes `from_meeting_id` | `recaps` |

#### Required lookup helpers

Atelier needs to resolve target row IDs to their `index_id`s before building the relation:

```python
# scripts/backend_memex.py
def _index_id_for_atelier_row(*, target_table: str, row_id: int) -> str | None:
    """Look up the index_id for an Atelier-store row by its target-table PK.

    Reads ~/.memex/index.db.documents WHERE store='atelier'
    AND table_name=<target_table> AND row_id=<row_id>. Returns None if the
    row exists but was never indexed (Tier 1 inserts), or if missing.
    """
```

`write_task` calls `_index_id_for_atelier_row(target_table="projects", row_id=project_id)` to build the `part_of` relation. One extra Index read per Tier 2 write. Cached per-process for hot project lookups (a single session typically touches one project).

If the lookup returns `None` (target row exists but was Tier 1-only — unlikely for projects, possible for sessions), the relation is silently skipped. Logged at debug.

### 6.10 Memex-side capabilities to verify before Plan 2 execution

Three contract details that Atelier depends on but the v2.2.0 contract brief didn't explicitly confirm. Plan 2 Task 1 cannot ship without these confirmed; flagged here so the implementing engineer checks first.

| # | Question | If yes | If no |
|---|---|---|---|
| 1 | Does `memex:brain:ask` / `memex:index:search` accept structured filters on `documents.metadata` JSON (e.g., `metadata.project_id = 7`)? | Cross-project queries work natively. | Atelier post-filters results in Python after a domain-restricted FTS5 hit; documented limitation. |
| 2 | Does `memex:index:search` traverse `relations` (e.g., "documents that supersede X", "tasks part_of project Y")? | Cross-document queries work natively. | Atelier issues a second query against the relations table directly via `memex:core:query` and unions client-side. |
| 3 | Does `documents.key` need to be unique, or only `index_id`? | Atelier can rely on the (`workspace/project/domain/date/title/seq`) format. | Atelier needs an alternate disambiguator — likely a `key_hash` suffix from `index_id`. |

Verify by reading Memex's `internal/index/search/SKILL.md` and `scripts/agents/reference_librarian.py`. If a capability is missing, file a Memex issue and document the Atelier-side workaround in `internal/memex/atelier-search-shims.md` before Plan 2 Task 3 (reads) implementation begins.

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

## 10. Workspace and project model

The brainstorm's "auto-detect from CWD" decision assumed **one repo = one project**. Real usage breaks that:

- Monorepos hold independent work streams (`apps/web`, `apps/api`, `apps/mobile`).
- A single repo can host multiple concurrent feature initiatives (`feature/auth-rewrite`, `feature/billing-v2`).
- A bug-fix campaign within one repo is its own logical project with its own design/plan/tasks.

Atelier therefore uses a **two-layer scope**: **workspace ≠ project**.

### 10.1 Layer definitions

| Layer | Definition | Cardinality | Identity |
|---|---|---|---|
| **Workspace** | A repository (git root). One per checked-out tree. | 1 per repo on disk; N per machine. | `repo_url` if remote present, else `realpath(git_root)`. Stored as `workspaces.slug` (a kebab-case derivation) + `workspaces.identity` (the full URL/path). |
| **Project** | A logical effort within a workspace. | 1..N per workspace; user-managed. | `(workspace_id, slug)`. |

Examples:

```
workspace        slug                  identity
─────────────    ─────────────────     ──────────────────────────────────────────
auth-service     auth-service          github.com/me/auth-service
billing          billing               github.com/me/billing-pipeline
acme-monorepo    acme-monorepo         github.com/acme/monorepo

projects         workspace             slug                description
─────────────    ─────────────────     ─────────────────   ─────────────────────────
1                auth-service          oauth-rewrite       OAuth2 refresh-token redo
2                billing               q2-cleanup          P0 hotpath migration
3                acme-monorepo         apps-web            web-app track
4                acme-monorepo         apps-api            API track
5                acme-monorepo         platform            cross-cutting infra work
```

### 10.2 Detection algorithm

```python
def resolve_scope() -> Scope:
    """Resolve (workspace, project) for the current command.

    Workspace: auto from CWD.
    Project:   from session state, falling back to the workspace's sole
               project if exactly one exists, otherwise prompt.
    """
    cwd = Path.cwd().resolve()
    git_root = find_git_root(cwd)
    if git_root is None:
        # not in a repo — workspace=None, only workspace-less ops permitted
        return Scope(workspace=None, project=None)

    identity = git_remote_url(git_root) or str(git_root)
    workspace = backend.find_or_create_workspace(identity=identity,
                                                  slug=slug_from(identity))

    state = read_session_state()   # ~/.atelier/state.json keyed by workspace.id
    current = state.get(workspace.id, {}).get("current_project")

    if current and project_exists(current):
        return Scope(workspace=workspace, project=current)

    projects = backend.list_projects(workspace_id=workspace.id)
    if len(projects) == 1:
        # Common case: workspace has one project — auto-select.
        write_session_state(workspace.id, current_project=projects[0].id)
        return Scope(workspace=workspace, project=projects[0])

    if len(projects) == 0:
        # First Atelier use in this repo — prompt.
        prompt_create_project(workspace)   # SKILL.md flow
        return resolve_scope()

    # Multiple projects, no current pointer — prompt.
    prompt_select_project(workspace, projects)
    return resolve_scope()
```

### 10.3 Session state

`~/.atelier/state.json` holds per-workspace "current project" pointers, indexed by workspace id:

```json
{
  "workspaces": {
    "1": { "current_project": 3, "set_at": "2026-05-16T14:00Z" },
    "3": { "current_project": 4, "set_at": "2026-05-16T15:22Z" }
  }
}
```

Switching projects within a workspace is an explicit op: `atelier project switch <slug>` (a new internal procedure). No CWD-magic — once a workspace has multiple projects, the user picks deliberately, with the choice persisted until they pick again. Per-shell scoping isn't worth the complexity; the state file is single-current-project-per-workspace.

### 10.4 Workspace-less operations

Some commands don't need a workspace (e.g., listing all projects across all workspaces, or writing a workspace-less daily log). For these, `Scope(workspace=None, project=None)` is valid; backend writes accept `workspace_id=NULL` and `project_id=NULL`.

In the key format (§6.7), workspace-less docs use `_no-workspace_/(no-project)/...` (a literal placeholder). Rare; mainly for the daily-log domain.

### 10.5 Local mode

Local mode (`<project-root>/.ai/atelier.db`) collapses workspace and project: the workspace IS the project, because the local DB file is bound to one checkout. `workspaces` table still exists (one row) for schema parity; the `workspaces.id` foreign key on `projects` is satisfied with that single row. Multi-project semantics are a Memex-mode-only feature.

This matches the §7 design that "Local mode keeps roles + agents in the project-local DB" — it's a slim mode where machine-global affordances degrade gracefully.

## 11. Schema

Atelier's migrations live under `migrations/shared/` (consumed by both backends) and `migrations/local-only/` (consumed only by Local mode). The retrofit ships these additions on top of the existing v1.0.13 schema:

### 11.1 Migration layout

```
migrations/shared/
  001_initial_schema.sql       MODIFIED — drop CREATE TABLE for roles, agents
  002_sessions.sql             unchanged
  003_phases.sql               unchanged
  004_tasks_parallel.sql       unchanged
  005_soft_walls.sql           unchanged
  006_index_ids.sql            NEW — index_id columns on indexed tables
  007_workspaces.sql           NEW — workspaces table + projects.workspace_id FK
  008_subdomains.sql           NEW — subdomain TEXT columns where applicable
  009_doc_kinds.sql            NEW — project_documents schema clean-up (type column policy)

migrations/local-only/
  100_local_roles_agents.sql   NEW — Local-mode keeps roles + agents in atelier.db
```

Memex-mode bootstrap supplies only `shared/` to `memex:core:create-store`. Local-mode setup supplies both directories in order. The `migrations` tracking table inside each store guarantees idempotency.

### 11.2 New table — `workspaces`

```sql
-- migrations/shared/007_workspaces.sql
CREATE TABLE IF NOT EXISTS workspaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,   -- used in keys per §6.7
    identity    TEXT UNIQUE NOT NULL,   -- repo_url or realpath(git_root)
    name        TEXT NOT NULL,          -- human-readable
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workspaces_identity ON workspaces(identity);

ALTER TABLE projects ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
ALTER TABLE projects ADD COLUMN slug TEXT;  -- per-workspace project slug; (workspace_id, slug) unique by app convention
CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id);
CREATE INDEX IF NOT EXISTS idx_projects_workspace_slug ON projects(workspace_id, slug);
```

`projects.workspace_id` is nullable temporarily to allow the migration to land on existing data. Bootstrap (§5) backfills NULL workspace_ids by creating a synthetic workspace per distinct existing project, then a later migration can be authored to enforce NOT NULL. v1 of the retrofit accepts the nullable column.

### 11.3 New columns — `subdomain`

```sql
-- migrations/shared/008_subdomains.sql
ALTER TABLE tasks              ADD COLUMN subdomain TEXT;
ALTER TABLE meeting_minutes    ADD COLUMN subdomain TEXT;
ALTER TABLE project_documents  ADD COLUMN subdomain TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_subdomain    ON tasks(subdomain);
CREATE INDEX IF NOT EXISTS idx_meetings_subdomain ON meeting_minutes(subdomain);
CREATE INDEX IF NOT EXISTS idx_docs_subdomain     ON project_documents(subdomain);
```

`projects` and `adr`-flavored docs don't carry subdomain (atomic per §6.4); the column is omitted on those tables.

### 11.4 Schema clean-up — `project_documents`

```sql
-- migrations/shared/009_doc_kinds.sql
-- The existing `type` column carries the kind of document (design/plan/adr/
-- research/postmortem/log). Per §6.4, some of these now ship as their own
-- domain in the Memex Index. The Atelier-side `type` column stays as the
-- finer-grained label; it's also written to `documents.domain` for the
-- promoted kinds, or to `documents.domain = 'project_doc'` for unpromoted
-- kinds (plan, runbook, etc.).
--
-- No DDL change; this migration is documentation only (a comment-only file
-- recorded in the migrations table so future readers see the policy).
SELECT 1;  -- noop migration; the policy lives in scripts/domain_vocabulary.py
```

The `type` column policy moves to code (`scripts.domain_vocabulary.TYPE_TO_DOMAIN`) so the source of truth is testable and a future audit can roll up frequencies. SQL-level enforcement (e.g., CHECK constraint) is rejected — it makes future taxonomy evolution into a destructive migration.

### 11.5 Removed from Memex-mode store

The Memex-mode `atelier.db` has **no** `roles`/`agents` tables — those live in `~/.memex/agents.db` (§6.5). This is enforced by `migrations/shared/001_initial_schema.sql` having the CREATE TABLE statements stripped. Local mode adds them back via `migrations/local-only/100_local_roles_agents.sql`.

### 11.6 Required columns for Memex Index linkback

Tables that participate in Tier 2 writes need `index_id TEXT` columns (added in `006_index_ids.sql` per Plan 1 Task 5):

- `projects.index_id`
- `tasks.index_id`
- `meeting_minutes.index_id`
- `project_documents.index_id`

`workspaces.index_id` is **not** added — workspaces are pure scope containers; they don't carry a Memex Index document. (Open question: should they? Cross-workspace recall on workspace metadata isn't a common query — skip in v1.)

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
| **Workspace detection** | Monorepo fixture with two projects; assert `resolve_scope()` finds the right workspace + prompts for project when ambiguous; auto-selects when single project; persists session state |
| **Project switch** | `atelier project switch <slug>` updates state.json; subsequent commands operate on the new project |
| **Domain validation** | `assert_valid(domain)` accepts every entry in `DOMAINS`; rejects unknown values with the documented `ValueError` message |
| **Domain mapping invariant** | Every entry in `DOMAINS` has a target table in `_DOMAIN_TO_TABLE`; every target is a real Atelier table |
| **Subdomain soft validation** | Unknown subdomains accepted; canonical subdomains preserved; audit util can enumerate frequencies |
| **Key uniqueness** | Generate 50 docs same title/date/project/domain in a loop — assert seqs `1..50`, no collisions, prefix scan returns all |
| **Key format** | Assert keys match the documented pattern `<workspace>/<project>/<domain>/<date>-<title>-<seq>`; `(no-project)` placeholder appears when project is None |
| **Searchable text** | Index a long markdown body (10k chars) — FTS5 search for a phrase from the LAST page returns the document |
| **File-content indexing** | `create_document` with a real markdown file on disk — assert `searchable` contains words from the file, not just the filename |
| **File missing error** | `create_document` with a non-existent filename raises `FileNotFoundError` before any Memex call |
| **Relation: `task part_of project`** | After `write_task(project_id=X, ...)`, assert `index.db.relations` has a row from the task's `index_id` to the project's `index_id` with `rel_type="part_of"` |
| **Relation: caller-supplied `derives_from`** | `write_document(domain="adr", derives_from_design_id=Y, ...)` builds the `derives_from` edge |
| **Supersedes on content edit** | `update_document(doc_id, title=new_title)` writes a new Tier 2 entry and a `supersedes` relation from new → old |

## 13. Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | Atelier accidentally falls into Tier 3 (LLM dispatch) for routine writes | The tier mapping in §6.1–§6.3 is enforced in `backend_memex.py` — `write_task` / `write_meeting` / `write_document` / `write_project` ALWAYS take the Tier 2 path. Tier 3 is reachable only through Atelier's `/atelier:ingest` skill with `--external-prose` semantics. Unit test asserts no Tier 2 write reaches `librarian.build_prompt`. |
| 2 | Bootstrap fails mid-step (e.g., register-role succeeds, create-store fails) | Each step is idempotent; no marker is written until all steps complete. Next run retries. |
| 3 | Local mode + Memex mode drift in schemas over time | Single `migrations/shared/` directory is the source of truth for both backends. CI test asserts schema parity. |
| 4 | A user with both a local DB and a Memex install runs many projects from different working directories without migrating | Per-project markers solve this: each project's migration choice is recorded in `.ai/atelier.migrated` or `.ai/atelier.local-only`. Atelier prompts once per project. |
| 5 | Two backends to maintain forever | Acknowledged. Every new feature must work in both modes (or be explicitly Memex-only). The facade in `scripts/backend.py` is the single seam; new methods are added there with two impls. |
| 6 | Memex Core's `register-role` / `register-agent` must be safe to call against an already-seeded entry | Memex Core's spec advertises idempotency. The plan-writer must add an integration test that re-runs bootstrap and asserts no duplicate rows + no errors. If a non-idempotent surface is found, raise upstream against Memex before merging this retrofit. |
| 7 | Atelier-domain values are not in Memex Librarian's known taxonomy | Tier 2 writes bypass the Librarian LLM entirely (§6.2), so the taxonomy gap doesn't bite. Only Tier 3 (`/atelier:ingest` with external prose) would care; document the constraint there if Tier 3 ships. |
| 8 | Searchable text is silently incomplete (file content not indexed, body truncation) | §6.8 mandates full body + file-content reads + no truncation. Tests in §12 ("file-content indexing", "long body searchable") catch regressions. CI must run the search regression suite. |
| 9 | `key` collisions on same-title same-day docs | §6.7 builds keys from `(workspace, project, domain, date, title, seq)`. The seq allocator queries the Index per write. Tests in §12 verify 50 same-title docs get distinct seqs. |
| 10 | Workspace/project ambiguity blocks first Atelier command in a new repo | `resolve_scope()` auto-creates the workspace and auto-selects the sole project when there's only one. The prompt only fires on multi-project workspaces — uncommon enough that the friction is acceptable. |
| 11 | Memex Index doesn't support the metadata filtering / relation traversal Atelier queries assume | §6.10 documents the capability checks. If a capability is missing, Atelier falls back to a two-step query (FTS5 hit → Atelier-side filter via `memex:core:query`); documented in `internal/memex/atelier-search-shims.md`. No hard blocker — just slower queries. |
| 12 | Migration replay (§8) needs to walk new workspace + project + subdomain columns | Plan 4 migration tasks must order: (a) walk projects, build a synthetic workspace per distinct project, (b) replay projects, (c) replay tasks/meetings/docs in dependency order, (d) populate subdomain from existing `type` column heuristics. Crash-safety unchanged. |

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

1. Two migration directories (`migrations/shared/` + `migrations/local-only/`) — resolved in §11.1.
2. Where Atelier's shipped agent personas live as JSON in this repo — `templates/agents/*.json` per Plan 1 Task 4.
3. Exact dispatch wrapper API in `scripts/backend.py` — pure dict-in/dict-out; preserved facade signatures from current modules where possible.
4. Migration prompt UX — single y/N at the top of the next command (Plan 4 Task 2). Optionally also offer a dedicated `atelier:migrate` skill as a manual trigger; deferred to v1.1.
5. **Memex-side capability gaps (§6.10)** — Plan 2 Task 3 (reads) is BLOCKED until the three questions in §6.10 are answered by reading Memex's `internal/index/search/SKILL.md`. The plan-writer must add this as a Wave-1 precondition.
6. **Workspace identity hashing** — `identity` column uses `repo_url` if a remote exists, else `realpath(git_root)`. What if a user clones the same repo to two paths? Treat as one workspace (remote URL match) — but if the repo has no remote yet (fresh `git init`), the two checkouts become two workspaces. Edge case; document the rule, accept the corner.
7. **Project-switch UX** — `atelier project switch <slug>` is an internal procedure; the user-facing surface could be a flag on `/atelier:load` (`/atelier:load --project oauth-rewrite`) instead of a dedicated skill. Plan 4 picks.
8. **Existing data backfill on first bootstrap** — users upgrading from v1.0.13 have existing `projects` rows with no `workspace_id`. The §11.2 migration is non-destructive; bootstrap can backfill by inferring one workspace per distinct project (1:1 mapping), preserving v1 semantics. Plan 4 Task 1 (migration) handles this.

## 16. Wave structure (preview for `writing-plans`)

The implementation plan should be wave-based — independent tasks dispatched in parallel within a wave, sequential between waves. Sketch:

```
Wave 0 — Foundations                                       [all parallel]
  - Persistence facade signature (scripts/backend.py skeleton)
  - Mode-detection module
  - Atelier role + agent seed JSON in templates/
  - migrations/ split: shared/ + local-only/
  - NEW: workspaces table + projects.workspace_id (migration 007)
  - NEW: subdomain columns (migration 008)
  - NEW: domain vocabulary + subdomain catalog + type→domain mapping
        (scripts/domain_vocabulary.py + internal/memex/domain-vocabulary.md)
  - NEW: §6.10 Memex-side capability verification — read Memex's
        index/search SKILL.md, fill in internal/memex/atelier-search-shims.md
        with confirmed-or-shimmed behavior for metadata filtering +
        relation traversal + key uniqueness expectations

Wave 1 — Memex-mode write paths                            [parallel; depends W0]
  - internal/memex/dispatch-write (Tier 2 caller-built librarian_output)
  - internal/memex/dispatch-core (Tier 1 direct CRUD)
  - internal/bootstrap-memex (idempotent; requires Memex v2.2.0+)
  - scripts/backend_memex.py — Tier 2 doc writes (§6.2) with
    key construction (§6.7), full searchable text (§6.8), and
    relation building (§6.9)
  - scripts/backend_memex.py — Tier 1 state writes
  - scripts/backend_memex.py — reads (FTS5 + metadata filter shim if needed)
  - scripts/workspace_resolver.py — resolve_scope() implementation

Wave 1' — Local-mode write paths                           [parallel; depends W0]
  - internal/local/wiki-write
  - internal/local/wiki-search
  - internal/local/wiki-archive
  - internal/local/state-crud
  - Backend.* (Local impl, mirrors W1 signatures; single-workspace collapse)

Wave 2 — Business-logic rewrites                           [parallel; depends W1+W1']
  - Rewrite scripts/projects.py to use backend.*
  - Rewrite scripts/tasks.py
  - Rewrite scripts/documents.py — INCLUDING file-content read (§6.8)
  - Rewrite scripts/meetings.py
  - Rewrite scripts/sessions.py
  - Rewrite scripts/workflow.py
  - NEW: scripts/workspaces.py — workspace + project CRUD helpers
  - NEW: scripts/scope.py — resolve_scope() + session-state file management
  - Delete scripts/db.py
  - Update scripts/migrate.py (or retire it; bootstrap subsumes it in Memex mode)

Wave 3 — Migration                                         [serial; depends W2]
  - internal/migrate-local-to-memex
  - Per-project markers (.ai/atelier.migrated, .ai/atelier.local-only)
  - User-prompt UX in the entry skills
  - Crash-safety tests
  - NEW: workspace backfill for v1.0.13 → v1.1.0 upgrades (one synthetic workspace
        per distinct project; preserves v1 semantics)

Wave 4 — Surface + docs                                    [parallel; depends W2]
  - Update .claude-plugin/plugin.json (verify only 4 surfaced skills)
  - Update CLAUDE.md (drop v1 dependency check, document dual-mode +
    workspace/project model)
  - Update README.md
  - CHANGELOG.md entry
  - Bump version (Atelier 1.0.13 → 1.1.0 — new feature, backward-compatible)

Wave P — Packaging + release                               [last]
  - Run full test suite (including the new searchable-text + key-uniqueness
    + workspace-detection + relation-population tests)
  - Build dist bundle (if Atelier has one)
  - Tag + push
  - Update agora marketplace pin to new Atelier version
```

The plan-writer will refine these and produce per-wave acceptance criteria.

**Wave 0 precondition** (gates the entire plan): Memex-side capability verification per §6.10 must produce one of three outcomes per question: (a) confirmed-supported in v2.2.0+, (b) shim documented in `internal/memex/atelier-search-shims.md` with a measurable workaround, (c) blocker raised against Memex with an issue link — in which case Plan 2 Task 3 (reads) is parked until the upstream change ships.
