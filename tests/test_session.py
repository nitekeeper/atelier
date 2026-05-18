# tests/test_session.py
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate import apply_migrations
from scripts.session import (
    write_session, get_session, read_latest,
    list_sessions, update_session, prune_sessions,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed_baseline(db: str) -> int:
    """Seed workspace + role + agent so write_session has its FK targets.

    Plan 3 Task 5 routes through the backend facade, which resolves the DB
    from the workspace root (not the explicit ``db_path``). The other
    Wave-2 rewrites (projects.py / roles.py / agents.py — Tasks 2, 7, 8)
    will land their own facade routing; until then we seed via raw SQL
    against the v1.1.0 schema (same pattern as
    ``tests/test_backend_local_state.py``).
    """
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("test", "repo:test", "Test", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("pm", "PM role", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("pm-1", "PM Agent", role_id, "Expert PM", now, now),
    )
    conn.commit()
    conn.close()
    return ws_id


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    # backend_local resolves DB from the git workspace root, so we fabricate
    # one inside tmp_path and chdir into it.
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    # Pin Local mode — the dev host has Memex installed, which would
    # otherwise route the facade through backend_memex (spec §7).
    from scripts import mode_detector
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    _seed_baseline(str(db))
    return str(db)


@pytest.fixture
def project_id(db_path):
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    ws_id = conn.execute(
        "SELECT id FROM workspaces LIMIT 1"
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "test-project", "TestProject", "Test", "design:open",
         "pm-1", now, now),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def test_write_session_creates_row(db_path, project_id):
    session = write_session(
        db_path, project_id, "pm-1", "design:open", "in-progress",
        pm_notes="Starting fresh",
    )
    assert session["id"] is not None
    assert session["phase"] == "design:open"
    assert session["status"] == "in-progress"
    assert session["pm_notes"] == "Starting fresh"
    assert session["project_id"] == project_id


def test_write_session_stores_pre_diagnose_phase(db_path, project_id):
    session = write_session(
        db_path, project_id, "pm-1", "diagnose:open", "in-progress",
        pre_diagnose_phase="tdd:clean",
    )
    assert session["pre_diagnose_phase"] == "tdd:clean"


def test_read_latest_returns_most_recent(db_path, project_id):
    write_session(db_path, project_id, "pm-1", "design:open", "complete")
    write_session(db_path, project_id, "pm-1", "plan:open", "in-progress",
                  accomplished="Design approved")
    session = read_latest(db_path, project_id)
    assert session["phase"] == "plan:open"
    assert session["accomplished"] == "Design approved"


def test_read_latest_missing_project_returns_none(db_path):
    assert read_latest(db_path, 9999) is None


def test_list_sessions_returns_most_recent_first(db_path, project_id):
    write_session(db_path, project_id, "pm-1", "design:open", "complete")
    write_session(db_path, project_id, "pm-1", "plan:open", "complete")
    sessions = list_sessions(db_path, project_id)
    assert len(sessions) == 2
    assert sessions[0]["phase"] == "plan:open"


def test_list_sessions_respects_limit(db_path, project_id):
    for i in range(5):
        write_session(db_path, project_id, "pm-1", f"design:open", "complete",
                      pm_notes=f"session {i}")
    sessions = list_sessions(db_path, project_id, limit=3)
    assert len(sessions) == 3


def test_update_session_modifies_fields(db_path, project_id):
    session = write_session(db_path, project_id, "pm-1", "design:open", "in-progress")
    updated = update_session(db_path, session["id"],
                             status="complete", accomplished="Design done")
    assert updated["status"] == "complete"
    assert updated["accomplished"] == "Design done"


def test_update_session_rejects_unknown_fields(db_path, project_id):
    session = write_session(db_path, project_id, "pm-1", "design:open", "in-progress")
    # unknown fields silently ignored — ID should not change
    result = update_session(db_path, session["id"], unknown_field="value")
    assert result["id"] == session["id"]


def test_prune_sessions_keeps_n_most_recent(db_path, project_id):
    for i in range(5):
        write_session(db_path, project_id, "pm-1", "design:open", "complete",
                      pm_notes=f"session {i}")
    deleted = prune_sessions(db_path, project_id, keep=2)
    assert deleted == 3
    remaining = list_sessions(db_path, project_id)
    assert len(remaining) == 2


def test_prune_noop_when_fewer_than_keep(db_path, project_id):
    write_session(db_path, project_id, "pm-1", "design:open", "complete")
    deleted = prune_sessions(db_path, project_id, keep=5)
    assert deleted == 0
