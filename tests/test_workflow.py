# tests/test_workflow.py
"""Phase-machine semantics through scripts.workflow.

Post-Plan-3-Task-6: workflow routes writes through `backend.transition_phase`
and `backend.record_phase_bypass`. Catalog reads still go to the workspace DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import workflow
from scripts.migrate import apply_migrations


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _seed(db_path: str) -> dict:
    """Seed workspace + role + agent + project. Returns ids."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("auth", "repo:auth", "Auth", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("pm", "PM", now, now),
    )
    role_id = cur.lastrowid
    conn.execute(
        "INSERT INTO agents (id, name, role_id, profile, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("pm-1", "PM", role_id, "Expert PM", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "auth", "Auth", "OAuth2 login", "design:open", "pm-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"workspace_id": ws_id, "project_id": proj_id}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Build a fake git workspace with .ai/atelier.db migrated and seeded.

    Forces Local mode — the workflow rewire exercises the same code path
    on both backends, but the Memex backend depends on the real Memex
    install (and on `~/.memex`), so we pin Local for deterministic CI.
    Memex-mode coverage lives in `test_backend_memex_*.py`."""
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
    ids = _seed(str(db))
    return {"root": root, "db": str(db), **ids}


@pytest.fixture
def db_path(workspace):
    return workspace["db"]


@pytest.fixture
def project_id(workspace):
    return workspace["project_id"]


# ── get_phase / catalog reads ──────────────────────────────────────────────


def test_get_phase_returns_default_design_open(db_path, project_id):
    assert workflow.get_phase(db_path, project_id) == "design:open"


def test_get_phase_unknown_project_raises(db_path):
    with pytest.raises(workflow.WorkflowError, match="not found"):
        workflow.get_phase(db_path, 9999)


# ── advance_phase ──────────────────────────────────────────────────────────


def test_advance_phase_valid_transition(db_path, project_id):
    workflow.advance_phase(db_path, project_id, "design:approved")
    assert workflow.get_phase(db_path, project_id) == "design:approved"


def test_advance_phase_invalid_transition_raises(db_path, project_id):
    with pytest.raises(workflow.WorkflowError, match="Invalid transition"):
        workflow.advance_phase(db_path, project_id, "qa:approved")


def test_advance_to_diagnose_from_any_phase(db_path, project_id):
    # diagnose:open is allow_from_any — should succeed at design:open
    workflow.advance_phase(db_path, project_id, "diagnose:open")
    assert workflow.get_phase(db_path, project_id) == "diagnose:open"


def test_full_happy_path(db_path, project_id):
    path = [
        "design:approved",
        "plan:open",
        "plan:approved",
        "tdd:red",
        "tdd:green",
        "tdd:clean",
        "review:open",
        "review:approved",
        "security:open",
        "security:approved",
        "qa:open",
        "qa:approved",
        "handoff:complete",
    ]
    for phase in path:
        workflow.advance_phase(db_path, project_id, phase)
        assert workflow.get_phase(db_path, project_id) == phase


# ── catalog helpers ────────────────────────────────────────────────────────


def test_get_valid_transitions_from_design_open(db_path):
    transitions = workflow.get_valid_transitions(db_path, "design:open")
    assert transitions == ["design:approved"]


def test_diagnose_allow_from_any_is_true(db_path):
    assert workflow.is_allow_from_any(db_path, "diagnose:open") is True


def test_design_open_allow_from_any_is_false(db_path):
    assert workflow.is_allow_from_any(db_path, "design:open") is False


# ── check_gate (smoke; deep coverage lives in test_soft_walls) ─────────────


def test_check_gate_passes_when_phase_satisfies_required(db_path, project_id):
    workflow.advance_phase(db_path, project_id, "design:approved")
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert isinstance(result, workflow.GateResult)
    assert result.allowed is True


def test_check_gate_fails_softly_when_phase_unmet(db_path, project_id):
    # design:open does NOT satisfy dev:plan's requirement of design:approved.
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert result.allowed is False
    assert result.current_phase == "design:open"
    assert result.required_phase == "design:approved"


# ── Memex-mode catalog routing (T23 round-1 R1) ────────────────────────────


def test_catalog_query_routes_through_memex_module(monkeypatch):
    """In Memex mode, `_catalog_query` must resolve `stores` via
    `backend_memex._memex_module("stores")` (not the broken
    `from scripts import stores` pattern).

    This pins the regression fixed in T23 round-1: the old path called
    `_ensure_memex_importable()` then `from scripts import stores`,
    which never resolved because Memex's `stores` module is not on
    `sys.path` as a top-level package — it lives inside the Memex
    plugin tree and must be loaded via `_load_memex_module` (the
    file-path-based loader behind `_memex_module`).
    """
    from scripts import backend_memex, mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    calls: list[tuple[str, str, tuple]] = []

    class FakeStores:
        @staticmethod
        def query(store: str, sql: str, params: tuple):
            calls.append((store, sql, params))
            # Mimic memex_stores.query: row-like dicts.
            return [{"phase": "design:open"}]

    requested: list[str] = []

    def fake_memex_module(dotted: str):
        requested.append(dotted)
        assert dotted == "stores", f"_catalog_query must request 'stores', got {dotted!r}"
        return FakeStores

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    # Run through `get_phase` to exercise the full call site.
    phase = workflow.get_phase("ignored.db", 42)

    assert phase == "design:open"
    assert requested == ["stores"], "_catalog_query must route through backend_memex._memex_module"
    assert len(calls) == 1
    store, sql, params = calls[0]
    assert store == "atelier"
    assert "FROM projects" in sql
    assert params == (42,)
