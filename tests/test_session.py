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
        write_session(db_path, project_id, "pm-1", "design:open", "complete",
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


def test_prune_sessions_memex_mode_routes_through_memex_module(monkeypatch):
    """Regression: prune_sessions in Memex mode must reach the Memex
    ``stores`` module via ``backend_memex._memex_module("stores")`` —
    NOT via ``from scripts import stores`` (which does not exist).
    """
    from scripts import mode_detector, backend_memex
    import scripts.session as session_module

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    # Fake stores module recording calls
    delete_calls = []

    class _FakeStores:
        @staticmethod
        def delete(*, name, table, row_id):
            delete_calls.append({"name": name, "table": table,
                                 "row_id": row_id})

    module_calls = []

    def fake_memex_module(dotted):
        module_calls.append(dotted)
        return _FakeStores

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    # Fake the core query so we get back deterministic rows without
    # touching the real Memex store.
    query_calls = []

    def fake_query(*, store, table, where):
        query_calls.append({"store": store, "table": table, "where": where})
        return [
            {"id": 1, "project_id": 7},
            {"id": 2, "project_id": 7},
            {"id": 3, "project_id": 7},
            {"id": 4, "project_id": 7},
        ]

    monkeypatch.setattr(backend_memex, "_memex_core_query", fake_query)

    deleted = session_module.prune_sessions("ignored.db",
                                            project_id=7, keep=2)

    # Memex stores module was requested by the correct dotted name
    assert "stores" in module_calls
    # Query was issued for the right table/store
    assert query_calls == [{"store": "atelier", "table": "sessions",
                            "where": {"project_id": 7}}]
    # The two oldest rows (ids 1 and 2 — kept rows are ids 4 and 3) were
    # deleted via the fake stores module
    assert deleted == 2
    deleted_ids = sorted(call["row_id"] for call in delete_calls)
    assert deleted_ids == [1, 2]
    for call in delete_calls:
        assert call["name"] == "atelier"
        assert call["table"] == "sessions"
