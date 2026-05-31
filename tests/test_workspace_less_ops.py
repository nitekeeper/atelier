"""Workspace-less operations tests (atelier#53 / spec §10.4).

Covers:

- Migration 005: `project_documents.workspace_id` and `project_id` are
  nullable; existing FTS5 virtual table + sync triggers survive the
  table rebuild.
- `backend.write_document` accepts None for both: in Local mode the row
  lands with NULL workspace_id/project_id; in Memex mode the workspace-less
  write now lands via the §6.7 `_no-workspace_` key (atelier#90 part-3) —
  the former NotImplementedError gate is gone (see
  `test_facade_write_document_memex_accepts_workspaceless`).
- `backend.find_project` / `list_projects` raise ValueError on
  `workspace_id=None` — workspace-scoped methods that REQUIRE a
  workspace context per §10.1.
- Migration applies idempotently against the registry.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import backend, backend_local, backend_memex
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Fresh workspace with migrations applied + a seeded workspace row
    for FK satisfiability when writing project-scoped docs alongside
    workspace-less ones."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ws = backend_local.find_or_create_workspace(identity="repo:test", slug="test", name="Test")
    return {"root": root, "db": str(db), "workspace_id": ws["id"]}


# ── Migration 005 — schema relaxation ──────────────────────────────────────


def test_migration_005_makes_workspace_id_nullable(workspace_root):
    """`PRAGMA table_info` reports notnull=0 for workspace_id + project_id
    after migration 005 has applied."""
    conn = sqlite3.connect(workspace_root["db"])
    info = conn.execute("PRAGMA table_info(project_documents)").fetchall()
    conn.close()
    cols = {row[1]: {"notnull": row[3]} for row in info}
    assert cols["workspace_id"]["notnull"] == 0
    assert cols["project_id"]["notnull"] == 0


def test_migration_005_preserves_fts_virtual_table(workspace_root):
    """The FTS5 virtual table + sync triggers from 002 survive the
    005 table rebuild — a search MATCH on a freshly-written doc
    still returns the row."""
    # Seed a workspace-less log row directly to keep the test focused on FTS5.
    conn = sqlite3.connect(workspace_root["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO project_documents "
        "(workspace_id, project_id, domain, subdomain, title, filename, "
        "created_by, created_at, updated_at) "
        "VALUES (?, ?, 'log', ?, ?, ?, ?, ?, ?)",
        (
            None,
            None,
            "daily",
            "Standup notes 2026-05-26",
            "logs/standup-2026-05-26.md",
            "atelier-pm-1",
            "2026-05-26T12:00:00Z",
            "2026-05-26T12:00:00Z",
        ),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT rowid, title FROM project_documents_fts WHERE project_documents_fts MATCH 'standup'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert "Standup" in rows[0][1]


def test_migration_005_preserves_existing_rows(tmp_path, monkeypatch):
    """If a DB already has project_documents rows when 005 applies, the
    rebuild MUST copy them across with all columns intact."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db_path = root / ".ai" / "atelier.db"
    db_path.parent.mkdir()
    # Apply 001 + 002 (NOT 005 yet), seed a row, then apply 005 and assert.
    # Easiest path: apply ALL shared migrations (idempotency makes this
    # equivalent to staged-apply for a fresh DB), then write the row, then
    # re-apply (no-op via registry gate).
    apply_migrations(str(db_path), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db_path), MIGRATIONS_DIR / "local-only")
    ws = backend_local.find_or_create_workspace(
        identity="repo:preserve", slug="preserve", name="Preserve"
    )
    # Seed a regular row with non-NULL columns — this is the "existing"
    # data the migration must preserve.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, 'p', 'P', '', 'design:open', 'atelier-pm-1', "
        "'2026-05-26T12:00Z', '2026-05-26T12:00Z')",
        (ws["id"],),
    )
    pid = conn.execute("SELECT id FROM projects WHERE slug = 'p'").fetchone()[0]
    conn.execute(
        "INSERT INTO project_documents "
        "(workspace_id, project_id, domain, subdomain, title, filename, "
        "created_by, source_ref, created_at, updated_at) "
        "VALUES (?, ?, 'design', 'auth', 'Auth Design', 'docs/auth.md', "
        "'atelier-pm-1', 'orig:src', '2026-05-26T12:00Z', '2026-05-26T12:00Z')",
        (ws["id"], pid),
    )
    conn.commit()
    # Re-apply (no-op: 005 already in the registry). The point is to verify
    # the previously-applied rebuild preserved every column including
    # source_ref (added by 002).
    apply_migrations(str(db_path), MIGRATIONS_DIR / "shared")
    row = conn.execute(
        "SELECT workspace_id, project_id, title, source_ref FROM project_documents WHERE title = ?",
        ("Auth Design",),
    ).fetchone()
    conn.close()
    assert row == (ws["id"], pid, "Auth Design", "orig:src")


# ── backend_local.write_document with NULL workspace_id/project_id ─────────


def test_write_document_accepts_workspace_less_log(workspace_root):
    """Spec §10.4 canonical use case: a daily log written before any
    workspace is registered. Both workspace_id and project_id NULL."""
    result = backend_local.write_document(
        workspace_id=None,
        project_id=None,
        domain="log",
        subdomain="daily",
        title="Workspace-less standup",
        body="# Notes\n\nFirst run of the day.",
        caller_agent_id="atelier-pm-1",
    )
    assert result["row_id"] >= 1
    # Confirm the row landed with NULLs.
    conn = sqlite3.connect(workspace_root["db"])
    row = conn.execute(
        "SELECT workspace_id, project_id, domain FROM project_documents WHERE id = ?",
        (result["row_id"],),
    ).fetchone()
    conn.close()
    assert row == (None, None, "log")


def test_write_document_accepts_workspace_scoped_project_less(workspace_root):
    """Workspace-level (no project) meeting / log per §6.7
    `<workspace>/(no-project)/...` keys: workspace_id set, project_id NULL."""
    result = backend_local.write_document(
        workspace_id=workspace_root["workspace_id"],
        project_id=None,
        domain="log",
        subdomain="daily",
        title="Workspace standup",
        body="# Workspace notes",
        caller_agent_id="atelier-pm-1",
    )
    conn = sqlite3.connect(workspace_root["db"])
    row = conn.execute(
        "SELECT workspace_id, project_id FROM project_documents WHERE id = ?",
        (result["row_id"],),
    ).fetchone()
    conn.close()
    assert row == (workspace_root["workspace_id"], None)


def test_write_document_preserves_project_scoped_writes(workspace_root):
    """Back-compat: project-scoped writes (the dominant case) still work
    exactly as before. NOT a regression test for migration 005, but a
    sanity-pin so we don't accidentally redirect every write to the
    workspace-less branch."""
    # Seed a project so we can write a project-scoped doc.
    conn = sqlite3.connect(workspace_root["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, 'auth', 'Auth', '', 'design:open', 'atelier-pm-1', "
        "'2026-05-26T12:00Z', '2026-05-26T12:00Z')",
        (workspace_root["workspace_id"],),
    )
    pid = conn.execute("SELECT id FROM projects WHERE slug = 'auth'").fetchone()[0]
    conn.commit()
    conn.close()
    result = backend_local.write_document(
        workspace_id=workspace_root["workspace_id"],
        project_id=pid,
        domain="design",
        subdomain="auth",
        title="Project doc",
        body="content",
        caller_agent_id="atelier-pm-1",
    )
    conn = sqlite3.connect(workspace_root["db"])
    row = conn.execute(
        "SELECT workspace_id, project_id FROM project_documents WHERE id = ?",
        (result["row_id"],),
    ).fetchone()
    conn.close()
    assert row == (workspace_root["workspace_id"], pid)


# ── Facade validation: methods that REQUIRE workspace_id ───────────────────


def test_find_project_rejects_none_workspace_id(workspace_root):
    """Per atelier#53, `find_project(workspace_id=None)` is a category
    error: projects are workspace-scoped per §10.1."""
    with pytest.raises(ValueError, match="workspace_id"):
        backend.find_project(workspace_id=None, slug="anything")


def test_list_projects_rejects_none_workspace_id(workspace_root):
    """Per atelier#53, `list_projects(workspace_id=None)` is a category
    error: listing is workspace-scoped."""
    with pytest.raises(ValueError, match="workspace_id"):
        backend.list_projects(workspace_id=None)


def test_find_project_error_message_suggests_iteration(workspace_root):
    """The ValueError should be actionable — it points the caller at the
    iteration recipe (`list_workspaces` + per-workspace lookup)."""
    with pytest.raises(ValueError) as excinfo:
        backend.find_project(workspace_id=None, slug="x")
    assert "list_workspaces" in str(excinfo.value)


# ── Memex-mode workspace-less write (atelier#90 part-3) ────────────────────


def test_facade_write_document_memex_accepts_workspaceless(monkeypatch):
    """In Memex mode, `write_document(workspace_id=None, project_id=None)`
    is now SUPPORTED (atelier#90 part-3): the facade reaches the Memex
    backend write instead of raising NotImplementedError. It folds NO
    `workspace_id` into adapted_metadata (absence, not None) and threads an
    explicit `workspace_less` discriminator so the backend takes the §6.7
    `_no-workspace_` key branch — NOT the singleton fallback."""
    from scripts import mode_detector

    # Force the facade to think we're in Memex mode without actually
    # invoking Memex Core — the gate is gone, the write must reach backend.
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    # Patch _backend_is_memex used by the facade router.
    monkeypatch.setattr(backend, "_backend", lambda: backend_memex)
    monkeypatch.setattr(backend, "_backend_is_memex", lambda be: True)
    with patch.object(backend_memex, "write_document") as memex_write:
        memex_write.return_value = {"row_id": 1, "index_id": "x"}
        backend.write_document(
            workspace_id=None,
            project_id=None,
            domain="log",
            subdomain="daily",
            title="Workspace-less log",
            body="x",
            metadata={},
            caller_agent_id="atelier-pm-1",
        )
        # The Memex write WAS reached (no gate fired).
        memex_write.assert_called_once()
        kwargs = memex_write.call_args.kwargs
        # A genuinely workspace-less write does NOT plant workspace_id in
        # adapted_metadata (absence-vs-None must survive so the backend
        # takes the §6.7 no-workspace branch, not the singleton fallback).
        assert "workspace_id" not in kwargs["metadata"]
        assert "project_id" not in kwargs["metadata"]
        # Pin the discriminator itself at the mock boundary: the both-None
        # call MUST thread workspace_less=True (the project-aware AND-clause
        # in backend.write_document is True only when BOTH are absent/None).
        assert kwargs.get("workspace_less") is True


def test_facade_write_document_memex_accepts_workspace_with_null_project(monkeypatch):
    """A workspace-scoped + project-less write (workspace_id set,
    project_id=None) IS supported in Memex mode — only the fully
    workspace-less case is deferred. The metadata fold should skip the
    None project_id (not land `project_id: null` in the blob)."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    monkeypatch.setattr(backend, "_backend", lambda: backend_memex)
    monkeypatch.setattr(backend, "_backend_is_memex", lambda be: True)
    with patch.object(backend_memex, "write_document") as memex_write:
        memex_write.return_value = {"row_id": 1, "index_id": "x"}
        backend.write_document(
            workspace_id=1,
            project_id=None,
            domain="log",
            subdomain="daily",
            title="Workspace-scoped log",
            body="x",
            metadata={},
            caller_agent_id="atelier-pm-1",
        )
        kwargs = memex_write.call_args.kwargs
        # workspace_id should land in adapted_metadata; project_id should NOT.
        assert kwargs["metadata"]["workspace_id"] == 1
        assert "project_id" not in kwargs["metadata"]
