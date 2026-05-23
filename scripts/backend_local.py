# scripts/backend_local.py
"""Local-mode backend — Plan 2 Tasks 5-7.

Project-local SQLite at `<workspace-root>/.ai/atelier.db`. Document-shaped
writes land in the v1.1.0 tables (`projects`, `project_documents`,
`tasks`, `meeting_minutes`). Raw bodies are archived to
`<workspace-root>/.ai/raw/`. No Librarian, no embeddings, no
federated Memex Index — Local mode is the slim fallback (spec §7).

Schema reference: `migrations/shared/001_v110_schema.sql` +
`migrations/shared/002_source_ref_and_fts.sql` +
`migrations/local-only/050_local_roles_agents.sql`. All three must be applied
to the local DB before any backend_local method is called.

Connection convention: stdlib `sqlite3` direct, `PRAGMA foreign_keys=ON`
per-connection. `scripts/db.py` is intentionally NOT imported — Plan 3
deletes it (system-prompt gotcha #2) and Local-mode callers shouldn't
re-introduce the coupling. Each method opens a fresh connection, commits,
and closes — keeps the surface easy to reason about and matches the
short-lived nature of write operations.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Helpers ────────────────────────────────────────────────────────────────


def _workspace_root() -> Path:
    """Resolve the workspace root for the current process.

    Mirrors `scripts.workspace.workspace_root` but imports `find_git_root`
    directly from `scripts.git_utils` to avoid the libtmux side-effect that
    `scripts.workspace` triggers at import time (preflight check). This
    keeps `backend_local` importable in non-tmux test environments
    without monkey-patching.
    """
    from scripts.git_utils import find_git_root

    cwd = Path.cwd().resolve()
    root = find_git_root(cwd)
    if root is None:
        raise FileNotFoundError(
            f"not inside a git repository: {cwd}. backend_local requires "
            f"CWD to be under a git workspace (or a fake .git dir for tests)."
        )
    return root


def _local_db() -> str:
    db = _workspace_root() / ".ai" / "atelier.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return str(db)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    """Filesystem-safe kebab-case slug. Strips non-alphanumerics, collapses
    runs of separators, caps at 64 chars so filenames stay sane."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64] or "untitled"


def _conn() -> sqlite3.Connection:
    """Open a fresh connection with WAL + FK enforcement.

    Per-connection `PRAGMA foreign_keys=ON` is required — SQLite scopes the
    pragma to the connection, not the database (spec §11.2 note).
    """
    c = sqlite3.connect(_local_db())
    # PRAGMA journal_mode=WAL is a one-time-set per database, BUT we run it
    # on every connect as belt-and-suspenders: a fresh DB file (e.g. test
    # workspaces) would otherwise stay in rollback-journal mode until a
    # caller happens to issue the pragma. Idempotent, so cheap (Nit-3).
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


# Domain validation is the facade's responsibility; backend_local accepts
# any string for `domain`, `subdomain`, `status`, etc. (Nit-7). Adding
# validation here would split the source of truth across two layers.
def _checked_lastrowid(cur: sqlite3.Cursor) -> int:
    """Return `cur.lastrowid` or raise if SQLite reports None.

    `sqlite3.Cursor.lastrowid` is typed `int | None` and is documented to be
    None after a no-op INSERT (e.g. INSERT…ON CONFLICT DO NOTHING). We don't
    use those patterns, but the defensive check costs nothing and makes
    static type-checkers happy (Nit-6 from reviewer).
    """
    row_id = cur.lastrowid
    if row_id is None:
        raise RuntimeError("INSERT returned no lastrowid; statement no-oped unexpectedly")
    return row_id


def _archive_raw(body: str, title: str, *, root: Path | None = None) -> tuple[str, str]:
    """Archive `body` under `<workspace_root>/.ai/raw/`.

    Returns `(absolute_path, relative_path)` — the relative path (relative
    to the workspace root) is what we store in `project_documents.filename`
    so the DB rows survive checkout-rename.

    Filename = `<slug-of-title>-<sha256-prefix>.md`. The hash prefix gives
    de-duplication when two writes share a title; the slug keeps the
    filename human-skimable. We shard on the first two hex chars so the
    directory doesn't grow unbounded.

    Writes go via `<final>.tmp` + `os.replace` so a crash mid-write leaves
    the canonical path either absent or fully populated — never partial
    (Nit-2 from reviewer).
    """
    if root is None:
        root = _workspace_root()
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    raw_dir = root / ".ai" / "raw" / h[:2]
    raw_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_slug(title)}-{h[:8]}.md"
    abs_path = raw_dir / fname
    if not abs_path.exists():
        tmp_path = abs_path.with_suffix(abs_path.suffix + ".tmp")
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, abs_path)
    rel_path = str(abs_path.relative_to(root))
    return str(abs_path), rel_path


# ── Document-shaped writes — Tier 2 ────────────────────────────────────────


def write_document(
    *,
    workspace_id: int,
    project_id: int,
    domain: str,
    subdomain: str | None,
    title: str,
    body: str,
    caller_agent_id: str,
    metadata: dict[str, Any] | None = None,
    source_url: str | None = None,
    source_ref: str | None = None,
    relations: Sequence[dict] = (),
) -> dict:
    """Persist a project_documents row.

    The v1.1.0 schema stores documents as pointer-rows referencing a
    markdown file on disk (`project_documents.filename`). We archive
    `body` to `.ai/raw/<key>.md` and record the relative path —
    the raw archive doubles as both the recoverable source-of-truth and
    the filesystem co-located copy spec §6.8 talks about.

    `source_url`, `relations`, and the `metadata` dict are accepted for
    signature parity with `backend.write_document` (the facade) and with
    the Memex backend, but Local mode currently no-ops on them: no
    relations table exists in the slim schema. `metadata` defaults to None
    (Nit-1) for caller ergonomics.

    `source_ref` is persisted to `project_documents.source_ref` (added in
    `002_source_ref_and_fts.sql`) so Plan 4's idempotent migrator can find
    the local row id for a v1.0.13 source key.
    """
    root = _workspace_root()
    _, rel_path = _archive_raw(body or "", title, root=root)
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO project_documents (workspace_id, project_id, "
            "domain, subdomain, title, filename, created_by, source_ref, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                project_id,
                domain,
                subdomain,
                title,
                rel_path,
                caller_agent_id,
                source_ref,
                _now(),
                _now(),
            ),
        )
        row_id = _checked_lastrowid(cur)
        c.commit()
        row = c.execute("SELECT * FROM project_documents WHERE id = ?", (row_id,)).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    # Mirror the facade's return shape: callers expect `row_id` + `index_id`
    # so they can detect Local-vs-Memex without sniffing the dict.
    result["row_id"] = row_id
    result["index_id"] = None  # Local mode has no global Memex index.
    return result


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
) -> dict:
    """Persist a row into the v1.1.0 `tasks` table.

    Tasks live in their own table (`tasks`), NOT in `project_documents`
    — domain routing happens at the facade layer, not by storing every
    write as a document. This matches spec §6.2 / §11.2 where each
    domain has a dedicated table.

    Note: `tasks` does NOT have `workspace_id` in v1.1.0 — the column
    is inherited transitively through `tasks.project_id → projects.workspace_id`.
    The kwarg is accepted for facade-signature parity but no DB column
    receives it.

    `source_ref` is persisted to `tasks.source_ref` (added in
    `002_source_ref_and_fts.sql`) so Plan 4's idempotent migrator can find
    the local row id for a v1.0.13 source key.
    """
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO tasks (project_id, title, description, subdomain, "
            "status, priority, notes, created_by, assigned_to, source_ref, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                title,
                description,
                subdomain,
                priority,
                notes,
                created_by,
                assigned_to,
                source_ref,
                _now(),
                _now(),
            ),
        )
        row_id = _checked_lastrowid(cur)
        c.commit()
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (row_id,)).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    result["row_id"] = row_id
    result["index_id"] = None
    return result


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
    skip_md: bool = False,
) -> dict:
    """Persist a row into `meeting_minutes` and write the markdown file.

    The on-disk markdown at `.ai/meetings/<date>-<slug>.md` is the
    human-facing artifact; the DB row carries structured metadata
    (date, subdomain, decisions) for search and joins. Both must land —
    if the file write succeeds but the DB insert fails, the next run
    will re-create the file (filename is deterministic).

    Per v1.1.0 schema, `meeting_minutes.project_id` is nullable so
    workspace-level meetings (no project) are supported.

    `source_ref` is persisted to `meeting_minutes.source_ref` (added in
    `002_source_ref_and_fts.sql`) so Plan 4's idempotent migrator can find
    the local row id for a v1.0.13 source key.

    `skip_md`: when True, the .md file write is suppressed. Callers in
    `scripts/meetings.py:create_meeting` already render a richer markdown
    (with optional Participants block) before invoking this backend; the
    flag prevents this method's participants-blind renderer from
    last-writer-wins clobbering that file. The DB row + `filename` value
    are still produced exactly as in the normal path.
    """
    root = _workspace_root()
    filename = f"{date}-{_slug(title)}.md"
    meetings_dir = root / ".ai" / "meetings"
    meetings_dir.mkdir(parents=True, exist_ok=True)
    if not skip_md:
        body = (
            f"# {title}\n\nDate: {date}\n\n## Summary\n\n{summary}\n\n## Decisions\n\n{decisions}\n"
        )
        (meetings_dir / filename).write_text(body, encoding="utf-8")
    # Store filename as workspace-relative for parity with project_documents.
    rel_filename = str((meetings_dir / filename).relative_to(root))
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO meeting_minutes (workspace_id, project_id, title, "
            "date, subdomain, filename, summary, decisions, created_by, "
            "source_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                project_id,
                title,
                date,
                subdomain,
                rel_filename,
                summary,
                decisions,
                created_by,
                source_ref,
                _now(),
                _now(),
            ),
        )
        row_id = _checked_lastrowid(cur)
        c.commit()
        row = c.execute("SELECT * FROM meeting_minutes WHERE id = ?", (row_id,)).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    result["row_id"] = row_id
    result["index_id"] = None
    return result


def write_project(
    *, workspace_id: int, slug: str, name: str, description: str, created_by: str
) -> dict:
    """Create a new project row scoped to `workspace_id`.

    v1.1.0 `(workspace_id, slug)` is UNIQUE so the local `slug` collides
    only within its workspace — matches spec §10.1's two-layer scope.
    Default phase `'design:open'` matches the schema default; we pass it
    explicitly so the row is fully populated regardless of SQLite's
    DEFAULT-clause behaviour under FK pragmas.
    """
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO projects (workspace_id, slug, name, description, "
            "phase, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'design:open', ?, ?, ?)",
            (workspace_id, slug, name, description, created_by, _now(), _now()),
        )
        row_id = _checked_lastrowid(cur)
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id = ?", (row_id,)).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    result["row_id"] = row_id
    result["index_id"] = None
    return result


# ── Operational state — Tier 1 ─────────────────────────────────────────────


def _workspace_id_for_project(c: sqlite3.Connection, project_id: int) -> int:
    """Derive `workspace_id` from `project_id` via the projects FK chain.

    Used by `upsert_session`: the v1.1.0 `sessions.workspace_id` is NOT
    NULL but the facade signature accepts only `(project_id, agent_id)`.
    We resolve the workspace through `projects.workspace_id` rather than
    making the caller plumb it through — cheaper than a signature change
    and matches the Memex backend, which faces the same problem.
    """
    row = c.execute("SELECT workspace_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"project_id={project_id} not found — cannot derive workspace_id")
    return row["workspace_id"]


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
    """Idempotent session upsert for `(project_id, agent_id, status='in-progress')`.

    The schema doesn't enforce uniqueness on `(project_id, agent_id)` — a
    pair can legitimately have many closed sessions plus at most one
    in-progress session. Upsert matches against the in-progress session
    so reopened work continues a single thread rather than spawning a
    new row.

    Only non-None fields overwrite the existing row; this is the
    "patch-style" upsert spec §6.1 documents.
    """
    c = _conn()
    try:
        ws_id = _workspace_id_for_project(c, project_id)
        existing = c.execute(
            "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? "
            "AND status = 'in-progress' LIMIT 1",
            (project_id, agent_id),
        ).fetchone()
        if existing is not None:
            # Per-column static SQL. Each branch is a hardcoded literal so
            # the column identity comes from this file, never from kwargs.
            now = _now()
            existing_id = existing["id"]
            if phase is not None:
                c.execute(
                    "UPDATE sessions SET phase = ?, updated_at = ? WHERE id = ?",
                    (phase, now, existing_id),
                )
            if current_tasks is not None:
                c.execute(
                    "UPDATE sessions SET current_tasks = ?, updated_at = ? WHERE id = ?",
                    (current_tasks, now, existing_id),
                )
            if accomplished is not None:
                c.execute(
                    "UPDATE sessions SET accomplished = ?, updated_at = ? WHERE id = ?",
                    (accomplished, now, existing_id),
                )
            if next_action is not None:
                c.execute(
                    "UPDATE sessions SET next_action = ?, updated_at = ? WHERE id = ?",
                    (next_action, now, existing_id),
                )
            if pm_notes is not None:
                c.execute(
                    "UPDATE sessions SET pm_notes = ?, updated_at = ? WHERE id = ?",
                    (pm_notes, now, existing_id),
                )
            # status is a kwarg with a default, so we have to be careful
            # not to flip an already-closed session back to in-progress
            # accidentally. Only update if the caller asked for a status
            # different from what's already there.
            if status != existing["status"]:
                c.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, existing_id),
                )
            c.commit()
            row = c.execute("SELECT * FROM sessions WHERE id = ?", (existing_id,)).fetchone()
        else:
            now = _now()
            cur = c.execute(
                "INSERT INTO sessions (workspace_id, project_id, agent_id, "
                "phase, current_tasks, accomplished, next_action, status, "
                "pm_notes, opened_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ws_id,
                    project_id,
                    agent_id,
                    phase,
                    current_tasks,
                    accomplished,
                    next_action,
                    status,
                    pm_notes,
                    now,
                    now,
                    now,
                ),
            )
            new_id = _checked_lastrowid(cur)
            c.commit()
            row = c.execute("SELECT * FROM sessions WHERE id = ?", (new_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else {}


def transition_phase(
    *, project_id: int, to_phase: str, agent_id: str, bypass_reason: str | None = None
) -> dict:
    """Advance `projects.phase`.

    Spec §3 (soft walls): the facade-level `transition_phase` does NOT
    validate the transition graph — that's `scripts/workflow.py:advance_phase`'s
    job. This backend method is the lowest-level write and trusts the
    caller. `bypass_reason` is accepted for signature parity but logging
    the bypass row is the caller's responsibility (`record_phase_bypass`).

    Returns the updated projects row. `agent_id` is in the signature for
    audit-trail parity with the Memex backend but Local-mode `projects`
    doesn't have a `phase_changed_by` column — kwarg accepted, no DB write.

    Raises ValueError if `project_id` doesn't exist — matches `upsert_session`'s
    error contract (Nit-4 from reviewer: error contracts must be uniform).
    """
    c = _conn()
    try:
        c.execute(
            "UPDATE projects SET phase = ?, updated_at = ? WHERE id = ?",
            (to_phase, _now(), project_id),
        )
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    finally:
        c.close()
    if row is None:
        raise ValueError(f"project_id={project_id} not found")
    return dict(row)


def update_task_status(*, task_id: int, status: str, notes: str | None = None) -> dict:
    """Set `tasks.status` (and optionally `tasks.notes`).

    `claimed_at` / `completed_at` are touched when the new status implies
    that transition — a tight contract with `scripts/tasks.py`'s
    assign/claim/complete flow so the timestamps stay coherent regardless
    of which call site updates the row. Otherwise we leave them alone
    so a status flip back from 'complete' → 'in-progress' doesn't
    clobber the original completion timestamp.

    Raises ValueError if `task_id` doesn't exist (Nit-4 from reviewer).
    """
    now = _now()
    c = _conn()
    try:
        # Per-column static SQL — every UPDATE statement below is a
        # hardcoded literal, no dynamic SET-clause construction.
        c.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id),
        )
        if notes is not None:
            c.execute(
                "UPDATE tasks SET notes = ?, updated_at = ? WHERE id = ?",
                (notes, now, task_id),
            )
        # claimed_at / completed_at use COALESCE so the FIRST transition
        # wins — flipping a complete task back to in-progress doesn't
        # clobber its original completion timestamp.
        if status == "in-progress":
            c.execute(
                "UPDATE tasks SET claimed_at = COALESCE(claimed_at, ?), updated_at = ? WHERE id = ?",
                (now, now, task_id),
            )
        elif status == "complete":
            c.execute(
                "UPDATE tasks SET completed_at = COALESCE(completed_at, ?), updated_at = ? WHERE id = ?",
                (now, now, task_id),
            )
        c.commit()
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        c.close()
    if row is None:
        raise ValueError(f"task_id={task_id} not found")
    return dict(row)


def record_phase_bypass(
    *, project_id: int, from_phase: str, to_phase: str, reason: str, agent_id: str
) -> dict:
    """Log a soft-wall bypass to `phase_bypasses`.

    The skill_gates table is advisory (spec §3) — bypasses ARE allowed,
    but they must be recorded so `internal/dev-handoff` can surface them
    in retros. Insert-only, no idempotency: each bypass invocation is a
    distinct audit event.
    """
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO phase_bypasses (project_id, from_phase, to_phase, "
            "reason, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, from_phase, to_phase, reason, agent_id, _now()),
        )
        new_id = _checked_lastrowid(cur)
        c.commit()
        row = c.execute("SELECT * FROM phase_bypasses WHERE id = ?", (new_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else {}


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
    """Search `project_documents` with optional scope filters.

    Text search uses the FTS5 virtual table `project_documents_fts`
    (added in `002_source_ref_and_fts.sql`) over (title, subdomain,
    filename). Structured filters (workspace_id / project_id / domain /
    subdomain) are orthogonal — they run as plain WHERE predicates on
    project_documents after the FTS5 join.

    Empty `query` returns the full filtered set; `query="*"` is treated
    the same way (consumer convention). When `query` is provided the
    `MATCH` syntax applies — callers can pass plain words ("auth") or
    FTS5 prefix queries ("auth*").

    Order is pinned `created_at DESC, id DESC` so paginated callers see
    a stable result set (Imp-2 from QA: limit-without-order is
    non-deterministic).
    """
    has_query = bool(query and query.strip() and query.strip() != "*")
    where: list[str] = []
    params: list[Any] = []
    if has_query:
        # Sub-select keeps FTS5 MATCH isolated from the structured WHERE.
        # FTS5 rowid is the project_documents.id by virtue of content_rowid.
        where.append(
            "id IN (SELECT rowid FROM project_documents_fts WHERE project_documents_fts MATCH ?)"
        )
        params.append(query)
    if workspace_id is not None:
        where.append("workspace_id = ?")
        params.append(workspace_id)
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if domain is not None:
        where.append("domain = ?")
        params.append(domain)
    if subdomain is not None:
        where.append("subdomain = ?")
        params.append(subdomain)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM project_documents {clause} ORDER BY created_at DESC, id DESC LIMIT ?"  # nosec B608
    params.append(limit)
    c = _conn()
    try:
        rows = c.execute(sql, params).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def get_task(*, task_id: int) -> dict | None:
    """Return the task row for `task_id` or None when missing."""
    c = _conn()
    try:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def list_tasks(*, project_id: int, status: str | None = None) -> list[dict]:
    """Return every task in `project_id`, optionally filtered by `status`."""
    c = _conn()
    try:
        if status is not None:
            rows = c.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND status = ? "
                "ORDER BY priority DESC, created_at",
                (project_id, status),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority DESC, created_at",
                (project_id,),
            ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def list_phase_bypasses(*, project_id: int) -> list[dict]:
    """Local-mode: SELECT all phase_bypasses rows for the project."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, project_id, from_phase, to_phase, reason, agent_id, created_at "
            "FROM phase_bypasses WHERE project_id = ? "
            "ORDER BY created_at",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── Cross-plan helpers ─────────────────────────────────────────────────────


def lookup_index_id_by_source_ref(*, source_ref: str) -> int | None:
    """Return the local row id for a given v1.0.13 `source_ref`, or None.

    Plan 4's idempotent migrator uses this to resume mid-replay: it
    annotates each v1.0.13 row with a stable `source_ref` (e.g.
    `"atelier:v1:project_documents:42"`), and on restart asks the local
    backend whether that source_ref has already been migrated. A hit
    means "skip, already inserted"; a miss means "insert and tag with
    this source_ref".

    Searches across all three migration-targeted tables in order of
    likelihood: project_documents → tasks → meeting_minutes. A source_ref
    is unique across all tables by Plan 4's naming convention; collisions
    are a caller-side programming error.

    The return value is a local row id (`int`), not a Memex index_id —
    Local mode has no federated index. Callers that need to distinguish
    Local from Memex should use `backend.mode` directly; this method
    answers a different question ("have I already migrated this row?").
    """
    if not source_ref:
        return None
    c = _conn()
    try:
        for table in ("project_documents", "tasks", "meeting_minutes"):
            row = c.execute(
                f"SELECT id FROM {table} WHERE source_ref = ? LIMIT 1",  # nosec B608
                (source_ref,),
            ).fetchone()
            if row is not None:
                return int(row["id"])
    finally:
        c.close()
    return None


def find_or_create_role(*, name: str, description: str) -> dict:
    """Return the local `roles` row for `name`, creating it if absent.

    Idempotent: a second call with the same `name` returns the existing
    row unchanged — `description` is NOT updated on hit (matches the
    Memex-mode contract; updates go through a separate `update_role`).

    Used by `scripts/seed_roles.py` (Plan 3) so the bootstrap path is
    safe to call on a populated DB.
    """
    c = _conn()
    try:
        existing = c.execute("SELECT * FROM roles WHERE name = ?", (name,)).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now()
        cur = c.execute(
            "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, description, now, now),
        )
        new_id = _checked_lastrowid(cur)
        c.commit()
        row = c.execute("SELECT * FROM roles WHERE id = ?", (new_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else {}


def find_or_create_agent(*, agent_id: str, name: str, role_id: int, profile: str) -> dict:
    """Return the local `agents` row for `agent_id`, creating it if absent.

    Idempotent: same shape as `find_or_create_role`. Local mode owns its
    own `agents` table (per `migrations/local-only/050_local_roles_agents.sql`);
    Memex mode defers to `~/.memex/agents.db`. The facade dispatches per
    mode so neither caller has to know which store backs the call.
    """
    c = _conn()
    try:
        existing = c.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now()
        c.execute(
            "INSERT INTO agents (id, name, role_id, profile, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, name, role_id, profile, now, now),
        )
        c.commit()
        row = c.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else {}
