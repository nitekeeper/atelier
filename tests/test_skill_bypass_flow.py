"""End-to-end integration test for the soft-wall bypass flow.

Exercises the full CLI surface: agent calls check-gate -> sees allowed=False
-> user confirms -> agent calls log-bypass -> later advance lifts the wall.
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
WORKFLOW_CLI = [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py")]


def _seed(db_path: str) -> int:
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("integ", "repo:integ", "Integ", None, now, now),
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
        ("agent-1", "Test", role_id, "", now, now),
    )
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "integ", "integration", "d", "design:open", "agent-1", now, now),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Forces Local mode (in-process AND in CLI subprocesses via fake HOME)."""
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
    return {"root": root, "db": str(db), "project_id": pid, "fake_home": str(fake_home)}


def _cli_env(project) -> dict:
    """Env for CLI subprocess: empty HOME so mode_detector picks Local."""
    # Strip USERPROFILE on Windows too (HOME alone isn't enough on Win).
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "1",
        "HOME": project["fake_home"],
    }
    env.pop("USERPROFILE", None)
    return env


def _check_gate_cli(project, skill):
    result = subprocess.run(
        WORKFLOW_CLI + [project["db"], "check-gate", str(project["project_id"]), skill],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_cli_env(project),
        cwd=str(project["root"]),
    )
    assert result.returncode == 0, f"check-gate failed: {result.stderr}"
    return json.loads(result.stdout)


def _log_bypass_cli(project, from_phase, to_phase, reason, agent_id):
    result = subprocess.run(
        WORKFLOW_CLI
        + [
            project["db"],
            "log-bypass",
            str(project["project_id"]),
            from_phase,
            to_phase,
            "--reason",
            reason,
            "--agent",
            agent_id,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_cli_env(project),
        cwd=str(project["root"]),
    )
    assert result.returncode == 0, f"log-bypass failed: {result.stderr}"
    return json.loads(result.stdout)


def test_full_bypass_flow(project):
    """Project at design:open; agent invokes dev:plan; bypass; advance; clean state."""
    db_path = project["db"]
    pid = project["project_id"]

    # 1. Agent checks gate for dev:plan while project is at design:open.
    result = _check_gate_cli(project, "dev:plan")
    assert result["allowed"] is False
    assert result["current_phase"] == "design:open"
    assert result["required_phase"] == "design:approved"

    # 2. User confirms bypass; agent calls log-bypass.
    bypass = _log_bypass_cli(
        project,
        from_phase=result["current_phase"],
        to_phase=result["required_phase"],
        reason="user explicitly approved out-of-phase plan work",
        agent_id="agent-1",
    )
    assert "bypass_id" in bypass and bypass["bypass_id"] > 0

    # 3. Bypass is recorded in phase_bypasses.
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT from_phase, to_phase, reason, agent_id FROM phase_bypasses WHERE id = ?",
            (bypass["bypass_id"],),
        ).fetchone()
    assert row["from_phase"] == "design:open"
    assert row["to_phase"] == "design:approved"
    assert row["agent_id"] == "agent-1"
    assert row["reason"] == "user explicitly approved out-of-phase plan work"

    # 4. Agent does explicit advancement.
    workflow.advance_phase(db_path, pid, "design:approved", agent_id="agent-1")

    # 5. Subsequent check-gate for dev:plan now returns allowed=True.
    result2 = _check_gate_cli(project, "dev:plan")
    assert result2["allowed"] is True
    assert result2["current_phase"] == "design:approved"

    # 6. Bypass row remains (audit trail).
    with closing(sqlite3.connect(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?",
            (pid,),
        ).fetchone()[0]
    assert count == 1


def test_bypass_aggregates_for_retro(project):
    """Multiple bypasses accumulate distinct rows; aggregate query groups them."""
    db_path = project["db"]
    pid = project["project_id"]

    # Two bypasses crossing the same wall (design:open -> design:approved).
    _log_bypass_cli(project, "design:open", "design:approved", "r1", "agent-1")
    _log_bypass_cli(project, "design:open", "design:approved", "r2", "agent-1")

    # Advance, then bypass crossing a different wall.
    workflow.advance_phase(db_path, pid, "design:approved", agent_id="agent-1")
    _log_bypass_cli(project, "design:approved", "plan:approved", "r3", "agent-1")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT from_phase, to_phase, COUNT(*) AS n
               FROM phase_bypasses
               WHERE project_id = ?
               GROUP BY from_phase, to_phase
               ORDER BY from_phase, to_phase""",
            (pid,),
        ).fetchall()
    grouped = [(r["from_phase"], r["to_phase"], r["n"]) for r in rows]
    # No idempotency window in v1.1.0 -- each invocation is a distinct row.
    assert grouped == [
        ("design:approved", "plan:approved", 1),
        ("design:open", "design:approved", 2),
    ]
