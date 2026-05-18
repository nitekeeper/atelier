# scripts/backend_local.py
"""Local-mode backend — Plan 2 Tasks 5-7.

Project-local SQLite at `<workspace-root>/.ai/atelier.db`. Document-shaped
writes land in the v1.1.0 tables (`projects`, `project_documents`,
`tasks`, `meeting_minutes`). Raw bodies are archived to
`<workspace-root>/.atelier/raw/`. No Librarian, no embeddings, no
federated Memex Index — Local mode is the slim fallback (spec §7).

Schema reference: `migrations/shared/001_v110_schema.sql` +
`migrations/local-only/050_local_roles_agents.sql`. Both must be applied
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
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


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
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def _archive_raw(body: str, title: str, *, root: Path | None = None) -> tuple[str, str]:
    """Archive `body` under `<workspace_root>/.atelier/raw/`.

    Returns `(absolute_path, relative_path)` — the relative path (relative
    to the workspace root) is what we store in `project_documents.filename`
    so the DB rows survive checkout-rename.

    Filename = `<slug-of-title>-<sha256-prefix>.md`. The hash prefix gives
    de-duplication when two writes share a title; the slug keeps the
    filename human-skimable. We shard on the first two hex chars so the
    directory doesn't grow unbounded.
    """
    if root is None:
        root = _workspace_root()
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    raw_dir = root / ".atelier" / "raw" / h[:2]
    raw_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_slug(title)}-{h[:8]}.md"
    abs_path = raw_dir / fname
    if not abs_path.exists():
        abs_path.write_text(body, encoding="utf-8")
    rel_path = str(abs_path.relative_to(root))
    return str(abs_path), rel_path


# ── Document-shaped writes — Tier 2 ────────────────────────────────────────

def write_document(*, workspace_id: int, project_id: int,
                   domain: str, subdomain: str | None,
                   title: str, body: str,
                   metadata: dict[str, Any], caller_agent_id: str,
                   source_url: str | None = None,
                   relations: Sequence[dict] = ()) -> dict:
    """Persist a project_documents row.

    The v1.1.0 schema stores documents as pointer-rows referencing a
    markdown file on disk (`project_documents.filename`). We archive
    `body` to `.atelier/raw/<key>.md` and record the relative path —
    the raw archive doubles as both the recoverable source-of-truth and
    the filesystem co-located copy spec §6.8 talks about.

    `source_url`, `relations`, and the `metadata` dict are accepted for
    signature parity with `backend.write_document` (the facade) and with
    the Memex backend, but Local mode currently no-ops on them: no
    relations table exists in the slim schema. Future work could store
    relations to a project-local `documents_relations` table if a
    consumer asks; until then they're accepted-and-ignored.
    """
    root = _workspace_root()
    _, rel_path = _archive_raw(body or "", title, root=root)
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO project_documents (workspace_id, project_id, "
            "domain, subdomain, title, filename, created_by, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, project_id, domain, subdomain, title, rel_path,
             caller_agent_id, _now(), _now()),
        )
        row_id = cur.lastrowid
        c.commit()
        row = c.execute(
            "SELECT * FROM project_documents WHERE id = ?", (row_id,)
        ).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    # Mirror the facade's return shape: callers expect `row_id` + `index_id`
    # so they can detect Local-vs-Memex without sniffing the dict.
    result["row_id"] = row_id
    result["index_id"] = None  # Local mode has no global Memex index.
    return result


def write_task(*, workspace_id: int, project_id: int,
               title: str, description: str,
               subdomain: str | None, created_by: str,
               assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None,
               relations: Sequence[dict] = ()) -> dict:
    """Persist a row into the v1.1.0 `tasks` table.

    Tasks live in their own table (`tasks`), NOT in `project_documents`
    — domain routing happens at the facade layer, not by storing every
    write as a document. This matches spec §6.2 / §11.2 where each
    domain has a dedicated table.

    Note: `tasks` does NOT have `workspace_id` in v1.1.0 — the column
    is inherited transitively through `tasks.project_id → projects.workspace_id`.
    The kwarg is accepted for facade-signature parity but no DB column
    receives it.
    """
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO tasks (project_id, title, description, subdomain, "
            "status, priority, notes, created_by, assigned_to, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (project_id, title, description, subdomain, priority, notes,
             created_by, assigned_to, _now(), _now()),
        )
        row_id = cur.lastrowid
        c.commit()
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (row_id,)).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    result["row_id"] = row_id
    result["index_id"] = None
    return result


def write_meeting(*, workspace_id: int, project_id: int | None,
                  title: str, date: str, summary: str,
                  decisions: str, subdomain: str | None,
                  created_by: str,
                  relations: Sequence[dict] = ()) -> dict:
    """Persist a row into `meeting_minutes` and write the markdown file.

    The on-disk markdown at `.ai/meetings/<date>-<slug>.md` is the
    human-facing artifact; the DB row carries structured metadata
    (date, subdomain, decisions) for search and joins. Both must land —
    if the file write succeeds but the DB insert fails, the next run
    will re-create the file (filename is deterministic).

    Per v1.1.0 schema, `meeting_minutes.project_id` is nullable so
    workspace-level meetings (no project) are supported.
    """
    root = _workspace_root()
    filename = f"{date}-{_slug(title)}.md"
    meetings_dir = root / ".ai" / "meetings"
    meetings_dir.mkdir(parents=True, exist_ok=True)
    body = (f"# {title}\n\nDate: {date}\n\n## Summary\n\n{summary}\n\n"
            f"## Decisions\n\n{decisions}\n")
    (meetings_dir / filename).write_text(body, encoding="utf-8")
    # Store filename as workspace-relative for parity with project_documents.
    rel_filename = str((meetings_dir / filename).relative_to(root))
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO meeting_minutes (workspace_id, project_id, title, "
            "date, subdomain, filename, summary, decisions, created_by, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, project_id, title, date, subdomain, rel_filename,
             summary, decisions, created_by, _now(), _now()),
        )
        row_id = cur.lastrowid
        c.commit()
        row = c.execute(
            "SELECT * FROM meeting_minutes WHERE id = ?", (row_id,)
        ).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    result["row_id"] = row_id
    result["index_id"] = None
    return result


def write_project(*, workspace_id: int, slug: str, name: str,
                  description: str, created_by: str) -> dict:
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
        row_id = cur.lastrowid
        c.commit()
        row = c.execute(
            "SELECT * FROM projects WHERE id = ?", (row_id,)
        ).fetchone()
    finally:
        c.close()
    result = dict(row) if row else {}
    result["row_id"] = row_id
    result["index_id"] = None
    return result
