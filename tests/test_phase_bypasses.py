"""Tests for the v1.1.0 phase_bypasses table and the log-bypass workflow command.

v1.1.0 schema: phase_bypasses(id, project_id, from_phase, to_phase, reason,
agent_id, created_at). The v1.0.13 columns (`skill`, `current_phase`,
`required_phase`, `note`, `bypassed_at`) and the 60-second idempotency
window are gone — `workflow.log_bypass` routes through
`backend.record_phase_bypass`, which is insert-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts import workflow
from scripts.migrate import apply_migrations

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _seed(db_path: str) -> int:
    """Seed workspace + role + agent + project. Returns project_id."""
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
        ("test-agent", "Test Agent", role_id, "Tester", now, now),
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
    """Fresh git workspace + migrated DB + seeded project. Forces Local mode.

    Returns (db_path, project_id, fake_home) -- fake_home so CLI subprocess
    tests can pass HOME and pin Local mode out-of-process too."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    pid = _seed(str(db))
    return str(db), pid, str(fake_home)


def test_log_bypass_writes_row(project):
    """log_bypass inserts a row and returns a positive integer id."""
    db_path, pid, _fake_home = project
    bypass_id = workflow.log_bypass(
        db_path,
        pid,
        from_phase="design:open",
        to_phase="plan:open",
        reason="user explicitly approved out-of-phase plan work",
        agent_id="test-agent",
    )
    assert isinstance(bypass_id, int) and bypass_id > 0

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT project_id, from_phase, to_phase, reason, agent_id "
            "FROM phase_bypasses WHERE id = ?",
            (bypass_id,),
        ).fetchone()
    assert row["project_id"] == pid
    assert row["from_phase"] == "design:open"
    assert row["to_phase"] == "plan:open"
    assert row["reason"] == "user explicitly approved out-of-phase plan work"
    assert row["agent_id"] == "test-agent"


def test_log_bypass_records_distinct_audit_events(project):
    """Each invocation is a distinct audit event — no idempotency window in v1.1.0."""
    db_path, pid, _fake_home = project
    first = workflow.log_bypass(
        db_path,
        pid,
        from_phase="design:open",
        to_phase="plan:open",
        reason="first attempt",
        agent_id="test-agent",
    )
    second = workflow.log_bypass(
        db_path,
        pid,
        from_phase="design:open",
        to_phase="plan:open",
        reason="second attempt",
        agent_id="test-agent",
    )
    assert first != second
    with closing(sqlite3.connect(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?", (pid,)
        ).fetchone()[0]
    assert count == 2


def test_log_bypass_writes_created_at(project):
    """v1.1.0 column `created_at` (renamed from `bypassed_at`) is populated."""
    db_path, pid, _fake_home = project
    bypass_id = workflow.log_bypass(
        db_path,
        pid,
        from_phase="design:open",
        to_phase="plan:open",
        reason="r",
        agent_id="test-agent",
    )
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT created_at FROM phase_bypasses WHERE id = ?", (bypass_id,)
        ).fetchone()
    assert row[0] is not None


def test_log_bypass_cli_writes_row(project, monkeypatch):
    """CLI log-bypass subcommand inserts a row and exits with code 0."""
    db_path, pid, fake_home = project
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "workflow.py"),
            db_path,
            "log-bypass",
            str(pid),
            "design:open",
            "plan:open",
            "--reason",
            "from CLI",
            "--agent",
            "test-agent",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1", "HOME": fake_home},
        cwd=str(Path(db_path).parent.parent),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output = json.loads(result.stdout)
    bypass_id = output["bypass_id"]
    assert isinstance(bypass_id, int) and bypass_id > 0

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT from_phase, to_phase, reason, agent_id FROM phase_bypasses WHERE id = ?",
            (bypass_id,),
        ).fetchone()
    assert row["from_phase"] == "design:open"
    assert row["to_phase"] == "plan:open"
    assert row["reason"] == "from CLI"
    assert row["agent_id"] == "test-agent"


def test_log_bypass_cli_missing_required_flag(project):
    """log-bypass without --reason exits 1."""
    db_path, pid, fake_home = project
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "workflow.py"),
            db_path,
            "log-bypass",
            str(pid),
            "design:open",
            "plan:open",
            "--agent",
            "test-agent",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1", "HOME": fake_home},
        cwd=str(Path(db_path).parent.parent),
    )
    assert result.returncode == 1
    assert "reason" in result.stderr.lower() or "requires" in result.stderr.lower()


def test_log_bypass_cli_missing_flag_value(project):
    """--agent without a value exits 1 and prints error to stderr."""
    db_path, pid, fake_home = project
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "workflow.py"),
            db_path,
            "log-bypass",
            str(pid),
            "design:open",
            "plan:open",
            "--agent",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1", "HOME": fake_home},
        cwd=str(Path(db_path).parent.parent),
    )
    assert result.returncode == 1
    assert "requires a value" in result.stderr
