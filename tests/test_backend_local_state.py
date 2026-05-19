"""Plan 2 Task 6 — Local-mode operational state writes.

Tests `backend_local.upsert_session` / `transition_phase` /
`update_task_status` / `record_phase_bypass` against the v1.1.0 schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed(db_path: str) -> dict:
    """Seed workspaces + roles + agents + project + task."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("myproj", "repo:myproj", "MyProj", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Product Manager", "PM", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("atelier-pm-1", "PM", role_id, "pm", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth", "d", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (proj_id, "Fix bug", "desc", "pending", "atelier-pm-1", now, now),
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"workspace_id": ws_id, "project_id": proj_id, "task_id": task_id}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ids = _seed(str(db))
    return {"root": root, "db": str(db), **ids}


# ── upsert_session ─────────────────────────────────────────────────────────


def test_upsert_session_inserts_when_new(workspace):
    s = backend_local.upsert_session(
        project_id=workspace["project_id"],
        agent_id="atelier-pm-1",
        phase="design:open",
    )
    assert s["id"] >= 1
    assert s["phase"] == "design:open"
    assert s["agent_id"] == "atelier-pm-1"
    assert s["status"] == "in-progress"


def test_upsert_session_updates_when_existing(workspace):
    first = backend_local.upsert_session(
        project_id=workspace["project_id"],
        agent_id="atelier-pm-1",
        phase="design:open",
    )
    second = backend_local.upsert_session(
        project_id=workspace["project_id"],
        agent_id="atelier-pm-1",
        accomplished="kickoff done",
    )
    # Same row updated, not a new insert.
    assert second["id"] == first["id"]
    assert second["accomplished"] == "kickoff done"
    # phase from first call is preserved.
    assert second["phase"] == "design:open"


# ── transition_phase ───────────────────────────────────────────────────────


def test_transition_phase_updates_projects_phase(workspace):
    """`transition_phase` updates `projects.phase` (NOT sessions.phase —
    the previous test name implied otherwise; corrected per C2 review).
    Lock the SQL contract: after the call, the projects row reads the
    new phase."""
    r = backend_local.transition_phase(
        project_id=workspace["project_id"],
        to_phase="plan:open",
        agent_id="atelier-pm-1",
    )
    assert r["phase"] == "plan:open"
    # Confirm DB-level write on the projects table.
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT phase FROM projects WHERE id = ?",
        (workspace["project_id"],),
    ).fetchone()
    conn.close()
    assert row["phase"] == "plan:open"


def test_transition_phase_raises_when_project_missing(workspace):
    """Nit-4: error contracts must be uniform — `upsert_session` raises
    on missing project_id, so `transition_phase` does too. Was silently
    returning {} pre-fix."""
    with pytest.raises(ValueError):
        backend_local.transition_phase(
            project_id=99999,
            to_phase="plan:open",
            agent_id="atelier-pm-1",
        )


# ── update_task_status ─────────────────────────────────────────────────────


def test_update_task_status_writes_status_and_timestamps(workspace):
    """Exercise both COALESCE branches: claimed_at on first 'in-progress'
    and completed_at on first 'complete'. Idempotent re-complete must not
    overwrite the timestamp (Imp-1 from QA)."""
    # First flip: pending → in-progress sets claimed_at.
    r1 = backend_local.update_task_status(
        task_id=workspace["task_id"],
        status="in-progress",
    )
    assert r1["status"] == "in-progress"
    assert r1["claimed_at"] is not None
    assert r1["updated_at"] is not None
    first_claimed_at = r1["claimed_at"]

    # Second flip: in-progress → complete sets completed_at; claimed_at unchanged.
    r2 = backend_local.update_task_status(
        task_id=workspace["task_id"],
        status="complete",
    )
    assert r2["status"] == "complete"
    assert r2["completed_at"] is not None
    assert r2["claimed_at"] == first_claimed_at  # COALESCE preserved
    first_completed_at = r2["completed_at"]

    # Idempotent re-complete: completed_at must NOT change.
    r3 = backend_local.update_task_status(
        task_id=workspace["task_id"],
        status="complete",
    )
    assert r3["completed_at"] == first_completed_at


def test_update_task_status_raises_when_task_missing(workspace):
    """Nit-4: error contract uniformity (mirror transition_phase)."""
    with pytest.raises(ValueError):
        backend_local.update_task_status(task_id=99999, status="complete")


# ── record_phase_bypass ────────────────────────────────────────────────────


def test_record_phase_bypass_inserts_row(workspace):
    r = backend_local.record_phase_bypass(
        project_id=workspace["project_id"],
        from_phase="design:open",
        to_phase="plan:open",
        reason="override",
        agent_id="atelier-pm-1",
    )
    assert r["id"] >= 1
    assert r["reason"] == "override"
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM phase_bypasses WHERE id = ?", (r["id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["from_phase"] == "design:open"
    assert row["to_phase"] == "plan:open"
    assert row["agent_id"] == "atelier-pm-1"
