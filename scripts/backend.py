# scripts/backend.py
"""Persistence facade — mode-dispatched.

Wave 0 shipped only the signatures (every body raised `NotImplementedError`).
Plan 2 Task 9 replaces the bodies with thin dispatchers: each method looks
up `mode_detector.detect_mode()` and forwards to either
`scripts/backend_memex.py` or `scripts/backend_local.py`.

Every method is keyword-only — Wave 0's contract — to prevent positional
drift between the two backends. Surface mirrors spec §4.3.

## Signature-drift adapter (write_document / write_task / write_meeting)

`backend_memex` exposes a narrower signature for the document-shaped writes
(no `workspace_id` / `subdomain` kwarg — those are folded into the Memex
Index's `metadata` blob by the librarian_output builder). `backend_local`
exposes the wide spec §4.3 signature (those are real DB columns there).
The facade is the wide signature (spec §4.3 is the canonical contract);
when dispatching to Memex, it folds the extra kwargs into `metadata`
before delegating. Local-mode is pure pass-through.

## Defense-in-depth: domain validation

The facade ALWAYS validates `domain` via `assert_valid_domain` before
dispatch so the unknown-domain path stays hermetic (no SQLite connect,
no Memex config read — callers see a clean `ValueError`). Memex
re-validates inside `backend_memex.write_document` as defense-in-depth,
so the validation contract holds even when callers bypass the facade.
Local mode does NOT re-validate inside `backend_local.write_document`:
the facade is the only entry point in the Atelier codebase, so an
extra validation pass would be redundant cost on the hot write path.
If a future caller wires `backend_local` directly, add `assert_valid_domain`
there at the top — keeping the rule "facade always validates" intact.

## Deferred to v1.2.0

Six methods stay raising `NotImplementedError`:
`find_or_create_workspace`, `find_workspace_by_identity`,
`list_workspaces`, `find_project`, `list_projects`, `get_document`.
Spec §4.3 keeps them on the surface (callers don't have to feature-flag);
Plan 2 defers their bodies. Re-implement them when the workspaces
script lands (Plan 3 / spec §10).
"""

from __future__ import annotations

from collections.abc import Sequence
from types import ModuleType
from typing import NoReturn

# ── Backend resolution ─────────────────────────────────────────────────────


def _backend() -> ModuleType:
    """Return the active backend module per `mode_detector.detect_mode()`.

    Cached per-call rather than at module import — the cache lives one
    layer down in `mode_detector` (single source of truth). This keeps
    the facade hot-reloadable in tests that monkey-patch `detect_mode`
    without forcing each test to reach into `backend._impl`.
    """
    from scripts import mode_detector

    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex

        return backend_memex
    from scripts import backend_local

    return backend_local


def _backend_is_memex(be: ModuleType) -> bool:
    """Compare backend module identity (not name string).

    Using `be is backend_memex` avoids brittle string matching on
    `be.__name__.endswith("backend_memex")` — that would also match
    e.g. a hypothetical `tests.fake_backend_memex` namespace and breaks
    if someone re-exports the module under an alias.
    """
    from scripts import backend_memex

    return be is backend_memex


def _not_implemented(name: str) -> NoReturn:
    raise NotImplementedError(
        f"backend.{name} is deferred to v1.2.0. Spec §4.3 keeps it on the "
        f"surface but Plan 2 does not implement it; see Plan 3 / spec §10."
    )


# ── Document-shaped writes — Tier 2 ────────────────────────────────────────


def write_project(
    *, workspace_id: int, slug: str, name: str, description: str, created_by: str
) -> dict:
    """Create a project row scoped to a workspace. Returns the new row.

    Symmetric across both backends — Memex builds the project's
    librarian_output and writes through Tier 2; Local stores a plain row
    in `projects`. The facade passes the kwargs through unchanged.
    """
    return _backend().write_project(
        workspace_id=workspace_id,
        slug=slug,
        name=name,
        description=description,
        created_by=created_by,
    )


def write_document(
    *,
    workspace_id: int | None,
    project_id: int | None,
    domain: str,
    subdomain: str | None,
    title: str,
    body: str,
    metadata: dict[str, object],
    caller_agent_id: str,
    source_url: str | None = None,
    source_ref: str | None = None,
    relations: Sequence[dict] = (),
) -> dict:
    """Persist a project document and any declared relations.

    Per spec §10.4 (atelier#53), `workspace_id` and `project_id` are
    BOTH nullable. The canonical use case is the daily-log domain,
    which may be workspace-less and/or project-less per §6.7's
    `_no-workspace_/(no-project)/...` key reservation. Local mode
    accepts both NULL (migration 005 relaxed the NOT NULL constraints).
    Memex mode raises `NotImplementedError` when `workspace_id is None`
    because the §6.7 key construction needs the literal `_no-workspace_`
    placeholder + Memex Index synthetic-workspace handling — out of
    scope for atelier#53 and tracked as a follow-up when a real
    workspace-less Memex consumer arrives.

    Memex-mode signature is narrower (no explicit `workspace_id` /
    `subdomain` kwargs); the adapter folds those into `metadata` so the
    canonical spec §4.3 wide signature stays the caller-facing contract.
    """
    from scripts.domain_vocabulary import assert_valid_domain

    assert_valid_domain(domain)
    be = _backend()
    if _backend_is_memex(be):
        if workspace_id is None:
            raise NotImplementedError(
                "write_document with workspace_id=None is not supported in "
                "Memex mode yet — the §6.7 key construction needs the "
                "literal `_no-workspace_` placeholder + synthetic-workspace "
                "handling in the Memex Index. Use Local mode for workspace-"
                "less daily logs, or open a follow-up issue when a Memex "
                "consumer arrives."
            )
        adapted_metadata = dict(metadata or {})
        # workspace_id / project_id / subdomain / source_ref belong on
        # the Index row's metadata blob in Memex mode — they're not
        # columns on the `documents` table, just searchable / filterable
        # fields. `setdefault` preserves caller-provided values, so a
        # caller passing `metadata={"workspace_id": 99}` wins over the
        # kwarg-level 42 — surprising at first but matches "caller knows
        # best" semantics already used for write_task/write_meeting.
        # None project_id is skipped from the fold so the metadata blob
        # doesn't carry an explicit `project_id: null` (it just remains
        # absent, which the Memex Index query plan reads as "any project").
        adapted_metadata.setdefault("workspace_id", workspace_id)
        if project_id is not None:
            adapted_metadata.setdefault("project_id", project_id)
        if subdomain is not None:
            adapted_metadata.setdefault("subdomain", subdomain)
        if source_ref is not None:
            adapted_metadata.setdefault("source_ref", source_ref)
        return be.write_document(
            domain=domain,
            title=title,
            body=body,
            metadata=adapted_metadata,
            caller_agent_id=caller_agent_id,
            source_url=source_url,
            relations=list(relations) if relations else None,
        )
    # Local mode accepts the wide signature directly, including NULL
    # workspace_id / project_id for §10.4 workspace-less / project-less
    # writes (migration 005).
    return be.write_document(
        workspace_id=workspace_id,
        project_id=project_id,
        domain=domain,
        subdomain=subdomain,
        title=title,
        body=body,
        metadata=metadata,
        caller_agent_id=caller_agent_id,
        source_url=source_url,
        source_ref=source_ref,
        relations=relations,
    )


def write_task(
    *,
    workspace_id: int,
    project_id: int,
    title: str,
    description: str,
    subdomain: str | None,
    created_by: str,
    assigned_to: str | None = None,
    priority: int = 0,
    notes: str | None = None,
    source_ref: str | None = None,
    relations: Sequence[dict] = (),
    parallel_group: int | None = None,
) -> dict:
    """Persist a task row and any declared relations.

    Memex-mode signature is narrower (no `workspace_id` / `subdomain`).
    `workspace_id` is dropped (singleton `_WORKSPACE_SLUG` for now; spec
    §10 multi-workspace lands in v1.2). `subdomain` is folded into the
    Memex Index row's metadata blob (matching `write_document`'s adapter
    pattern) so it survives into searchable storage rather than getting
    silently discarded.
    """
    be = _backend()
    if _backend_is_memex(be):
        adapted_metadata: dict = {}
        if subdomain is not None:
            adapted_metadata["subdomain"] = subdomain
        return be.write_task(
            title=title,
            description=description,
            project_id=project_id,
            created_by=created_by,
            assigned_to=assigned_to,
            priority=priority,
            notes=notes,
            source_ref=source_ref,
            metadata=adapted_metadata if adapted_metadata else None,
            relations=list(relations) if relations else None,
            parallel_group=parallel_group,
        )
    return be.write_task(
        workspace_id=workspace_id,
        project_id=project_id,
        title=title,
        description=description,
        subdomain=subdomain,
        created_by=created_by,
        assigned_to=assigned_to,
        priority=priority,
        notes=notes,
        source_ref=source_ref,
        relations=relations,
        parallel_group=parallel_group,
    )


def write_meeting(
    *,
    workspace_id: int,
    project_id: int | None,
    title: str,
    date: str,
    summary: str,
    decisions: str,
    subdomain: str | None,
    created_by: str,
    source_ref: str | None = None,
    relations: Sequence[dict] = (),
) -> dict:
    """Persist a meeting record (DB row + markdown payload) plus relations.

    `date` is ISO YYYY-MM-DD form. Same Memex-vs-Local signature drift
    as `write_task` — `workspace_id` is dropped on the Memex path (no
    DB column; spec §10 multi-workspace lands in v1.2). `subdomain` is
    folded into the Memex Index row's metadata blob (matching
    `write_document`'s adapter pattern) so it survives into searchable
    storage rather than getting silently discarded.
    """
    be = _backend()
    if _backend_is_memex(be):
        adapted_metadata: dict = {}
        if subdomain is not None:
            adapted_metadata["subdomain"] = subdomain
        return be.write_meeting(
            title=title,
            date=date,
            summary=summary,
            decisions=decisions,
            created_by=created_by,
            project_id=project_id,
            source_ref=source_ref,
            metadata=adapted_metadata if adapted_metadata else None,
            relations=list(relations) if relations else None,
        )
    return be.write_meeting(
        workspace_id=workspace_id,
        project_id=project_id,
        title=title,
        date=date,
        summary=summary,
        decisions=decisions,
        subdomain=subdomain,
        created_by=created_by,
        source_ref=source_ref,
        relations=relations,
    )


# ── Operational state — Tier 1 ─────────────────────────────────────────────


def upsert_session(
    *,
    project_id: int,
    agent_id: str,
    phase: str | None = None,
    current_tasks: str | None = None,
    accomplished: str | None = None,
    next_action: str | None = None,
    status: str = "in-progress",
    pm_notes: str | None = None,
) -> dict:
    """Idempotent session upsert for `(project_id, agent_id)`."""
    return _backend().upsert_session(
        project_id=project_id,
        agent_id=agent_id,
        phase=phase,
        current_tasks=current_tasks,
        accomplished=accomplished,
        next_action=next_action,
        status=status,
        pm_notes=pm_notes,
    )


def transition_phase(
    *, project_id: int, to_phase: str, agent_id: str, bypass_reason: str | None = None
) -> dict:
    """Advance the project phase.

    `bypass_reason` is accepted for signature parity; callers MUST log
    the bypass via `record_phase_bypass` BEFORE invoking this method so
    a transient failure between the two writes can be detected. Both
    backends ignore the kwarg.
    """
    return _backend().transition_phase(
        project_id=project_id,
        to_phase=to_phase,
        agent_id=agent_id,
        bypass_reason=bypass_reason,
    )


def update_task_status(*, task_id: int, status: str, notes: str | None = None) -> dict:
    """Set the task status. Returns the updated row."""
    return _backend().update_task_status(
        task_id=task_id,
        status=status,
        notes=notes,
    )


# Allowlist for `update_task`. Kept here (not on the backends) so the
# facade is the single source of truth for what columns are externally
# writable via the general-partial-update path.
#
# `status` is INTENTIONALLY EXCLUDED — status writes MUST go through
# `update_task_status` to preserve the lifecycle-timestamp side-effects
# (claimed_at / completed_at via COALESCE in backend_local). Routing a
# status write through this method would bypass those side-effects and
# leave the row in a coherent-but-incomplete state.
#
# `assign_task` is the only path that flips status as a side effect of
# an assignment write — general `update_task` never auto-flips status,
# even when `assigned_to` is one of the changes.
_UPDATE_TASK_ALLOWED_COLUMNS: frozenset[str] = frozenset(
    {"title", "description", "priority", "notes", "assigned_to", "parallel_group"}
)


def update_task(*, task_id: int, **changes: object) -> dict:
    """General partial update for a task row. Returns the updated row.

    Allowed columns: title, description, priority, notes, assigned_to.
    Unknown keys raise `ValueError` BEFORE either backend is touched
    (hermetic — no SQLite open, no Memex Core hit).

    Status writes are NOT accepted here — they must go through
    `update_task_status` to preserve lifecycle timestamps
    (claimed_at / completed_at). Passing `status` raises a dedicated
    `ValueError` so the caller sees a clear "route this through
    update_task_status" message rather than a generic "unknown column".

    Semantics: this is a *pure* column update. It does NOT auto-flip
    `status` to `'assigned'` when `assigned_to` is in the changes dict
    — that side effect is the exclusive contract of `assign_task`,
    which keeps the two methods semantically distinct.
    """
    if not changes:
        return _backend().update_task(task_id=task_id)
    if "status" in changes:
        raise ValueError(
            "status writes must go through update_task_status (preserves lifecycle timestamps)"
        )
    unknown = set(changes) - _UPDATE_TASK_ALLOWED_COLUMNS
    if unknown:
        raise ValueError(
            f"update_task: unknown column(s) {sorted(unknown)}; "
            f"allowed: {sorted(_UPDATE_TASK_ALLOWED_COLUMNS)}"
        )
    return _backend().update_task(task_id=task_id, **changes)


def delete_task(*, task_id: int) -> bool:
    """Delete a task row. Returns True on success, False if the row was
    absent (idempotent semantics matching SQLite `rowcount > 0`)."""
    return _backend().delete_task(task_id=task_id)


def assign_task(*, task_id: int, agent_id: str) -> dict:
    """Atomic two-field update: set `assigned_to = agent_id` AND flip
    `status = 'assigned'` in a single backend statement so the row can
    never be observed mid-update with one field set and the other not.
    Returns the updated row.

    This is the ONLY path on the backend surface that auto-flips
    status as a side effect of an assignment write. General
    `update_task` never does.
    """
    return _backend().assign_task(task_id=task_id, agent_id=agent_id)


def record_phase_bypass(
    *, project_id: int, from_phase: str, to_phase: str, reason: str, agent_id: str
) -> dict:
    """Log a soft-wall bypass to `phase_bypasses`. Returns the new row.
    Surfaced by `internal/dev-handoff` retros."""
    return _backend().record_phase_bypass(
        project_id=project_id,
        from_phase=from_phase,
        to_phase=to_phase,
        reason=reason,
        agent_id=agent_id,
    )


def list_phase_bypasses(*, project_id: int) -> list[dict]:
    """Return all phase_bypasses rows for a project.

    Returns raw rows from the phase_bypasses table (one dict per row) with
    keys: id, project_id, from_phase, to_phase, reason, agent_id, created_at.
    Returns [] if no bypasses exist for the project.

    Callers that need grouped/aggregated views (e.g. dev-handoff and
    dev-finish retros) aggregate in Python at the rendering layer — see
    those SKILL.md files for the canonical pattern.
    """
    return _backend().list_phase_bypasses(project_id=project_id)


# ── Workspace resolution — landed via atelier#51 (workspace layer) ─────────
#
# Spec §4.3 keeps these on the surface so callers (`scripts/scope.py`'s
# `resolve_scope()`, atelier#50) don't have to feature-flag. The
# project-layer + document-layer sibling stubs (find_project,
# list_projects, get_document) land via atelier#52.


def find_or_create_workspace(
    *, identity: str, slug: str, name: str, description: str | None = None
) -> dict:
    """Return the workspace row for `identity`, creating it if absent.

    Idempotent on `identity` (the §10.1 stable workspace identifier).
    The implementation uses INSERT-OR-IGNORE + SELECT in Local mode (race-
    safe under the SQLite WAL connection) and look-up-then-insert in
    Memex mode (single-user atelier; no concurrent workspace creation in
    practice). When two callers race to create the same workspace,
    both observe the same row on return.

    Per spec §10.1: `identity` is the canonical workspace key
    (`repo_url` if a git remote is configured, else `realpath(git_root)`),
    `slug` is the §0.2 kebab-case form used in §6.7 key construction,
    `name` is human-displayable.
    """
    return _backend().find_or_create_workspace(
        identity=identity, slug=slug, name=name, description=description
    )


def find_workspace_by_identity(*, identity: str) -> dict | None:
    """Return the workspace row for `identity` or None if absent."""
    return _backend().find_workspace_by_identity(identity=identity)


def list_workspaces() -> list[dict]:
    """Return every workspace row, ordered by slug."""
    return _backend().list_workspaces()


def find_project(*, workspace_id: int, slug: str) -> dict | None:
    """Return the project row for `(workspace_id, slug)` or None if absent.

    Spec §10.1 identity rule: a project's canonical key is
    `(workspace_id, slug)` — slug is unique WITHIN a workspace, not
    globally. `find_or_create_workspace` from atelier#51 lands the
    workspace-id; the caller (e.g. `scripts.scope.resolve_scope`)
    pairs it with the locally-known slug.

    Per atelier#53, `workspace_id=None` is REJECTED with a clear
    ValueError — projects are workspace-scoped per §10.1 and a
    workspace-less project lookup is a category error (not a
    deferred surface).
    """
    if workspace_id is None:
        raise ValueError(
            "find_project requires workspace_id — projects are "
            "workspace-scoped per spec §10.1. For a cross-workspace "
            "project search, the caller must iterate workspaces "
            "via `list_workspaces` and call `find_project` per workspace."
        )
    return _backend().find_project(workspace_id=workspace_id, slug=slug)


def list_projects(*, workspace_id: int) -> list[dict]:
    """Return every project row in the given workspace, ordered by slug.

    Spec §10.2 detection algorithm uses this in the "workspace has one
    project — auto-select" and "multiple projects — prompt user" arms
    of `resolve_scope`. Always workspace-scoped per the §10.1 two-layer
    invariant; no global cross-workspace listing here (a separate read
    surface lands later if/when a consumer needs it).

    Per atelier#53, `workspace_id=None` is REJECTED with a clear
    ValueError — cross-workspace listing is not the workspace-less
    surface §10.4 describes (that's for daily-log writes). Callers
    that need cross-workspace results should iterate
    `list_workspaces()` and call this per workspace.
    """
    if workspace_id is None:
        raise ValueError(
            "list_projects requires workspace_id — listing is "
            "workspace-scoped per spec §10.1. For cross-workspace "
            "results, iterate `list_workspaces()` and call this per "
            "workspace."
        )
    return _backend().list_projects(workspace_id=workspace_id)


# ── Reads ──────────────────────────────────────────────────────────────────


def find_documents(
    *,
    query: str,
    workspace_id: int | None = None,
    project_id: int | None = None,
    domain: str | None = None,
    subdomain: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text / metadata search over documents, optionally scoped to a
    workspace / project / domain. Returns ranked rows."""
    return _backend().find_documents(
        query=query,
        workspace_id=workspace_id,
        project_id=project_id,
        domain=domain,
        subdomain=subdomain,
        limit=limit,
    )


def get_task(*, task_id: int) -> dict | None:
    """Return the task row for `task_id` or None if absent."""
    return _backend().get_task(task_id=task_id)


def list_tasks(
    *, project_id: int, status: str | None = None, assigned_to: str | None = None
) -> list[dict]:
    """Return every task row in the project, optionally filtered by
    `status` and/or `assigned_to`. Both filters compose (AND); each is
    pushed into the backend WHERE clause rather than post-filtered in
    Python so we don't drag rows we'll throw away across the FFI."""
    return _backend().list_tasks(project_id=project_id, status=status, assigned_to=assigned_to)


def get_document(*, doc_id: int) -> dict | None:
    """Return the `project_documents` row for `doc_id` or None if absent.

    `doc_id` is the integer `project_documents.id` autoincrement column
    (Local) / the equivalent row id in the atelier-on-Memex
    `project_documents` table (Memex). NOT the Memex Index `index_id`
    UUID — that lookup is a separate surface (`lookup_index_id_by_source_ref`
    + future read surfaces).
    """
    return _backend().get_document(doc_id=doc_id)


def lookup_index_id_by_source_ref(*, source_ref: str) -> str | None:
    """Reverse-lookup for the idempotent-migration use case (Plan 4).

    Memex mode returns the Memex Index `index_id` (str) on hit; Local
    mode returns the local row id (int). Both are typed `str | None` at
    the facade because the caller (`migrate_to_memex.py`) treats it as
    an opaque "have I already migrated this row?" check — the concrete
    type doesn't matter, only the truthy / None distinction.
    """
    return _backend().lookup_index_id_by_source_ref(source_ref=source_ref)


# ── Idempotent role / agent helpers ────────────────────────────────────────
#
# Used by `scripts/seed_roles.py` (Plan 3) and the Memex-mode bootstrap.
# Both must be safe to call on a populated DB — return the existing row
# instead of raising IntegrityError.


def find_or_create_role(*, name: str, description: str) -> dict:
    """Return the role row with this `name`, creating it if absent.
    Idempotent."""
    return _backend().find_or_create_role(name=name, description=description)


def find_or_create_agent(*, agent_id: str, name: str, role_id: int, profile: str) -> dict:
    """Return the agent row with this `agent_id`, creating it if absent.
    Idempotent."""
    return _backend().find_or_create_agent(
        agent_id=agent_id,
        name=name,
        role_id=role_id,
        profile=profile,
    )
