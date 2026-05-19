"""Soft walls: check_gate returns GateResult instead of raising on phase mismatch."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import workflow
from scripts.migrate import apply_migrations


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _seed(db_path: str) -> int:
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("test", "repo:test", "Test", None, now, now),
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
        ("test-agent", "Test", role_id, "Tester", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "test", "Test", None, "design:open", "test-agent", now, now),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Forces Local mode -- Memex-mode soft-wall coverage lives in test_backend_memex_*."""
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
    pid = _seed(str(db))
    return str(db), pid


def test_check_gate_returns_allowed_when_ungated(project):
    """check_gate on an ungated skill returns GateResult(allowed=True) with no required_phase."""
    db_path, project_id = project
    # dev:design has skill_gates.required_phase = NULL -> no gate.
    result = workflow.check_gate(db_path, project_id, "dev:design")
    assert result.allowed is True
    assert result.current_phase == "design:open"
    assert result.required_phase is None
    assert "no gate" in result.reason.lower()


def test_check_gate_does_not_raise_on_phase_mismatch(project):
    """CLAUDE.md hard rule: check_gate never raises on phase mismatch -- returns GateResult(allowed=False)."""
    db_path, project_id = project
    # design:open does NOT satisfy dev:plan's gate (design:approved).
    result = workflow.check_gate(db_path, project_id, "dev:plan")
    assert result.allowed is False
    assert result.current_phase == "design:open"
    assert result.required_phase == "design:approved"
    assert "design:open" in result.reason
    assert "design:approved" in result.reason


def test_check_gate_raises_on_unknown_project_id(project):
    """Unknown project_id is a programming error -- the soft-wall rule doesn't cover it."""
    db_path, _pid = project
    with pytest.raises(workflow.WorkflowError, match="not found"):
        workflow.check_gate(db_path, 9999, "dev:plan")
