"""End-to-end integration test for the soft-wall bypass flow."""
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts import workflow
from scripts.agents import create_agent
from scripts.db import get_connection
from scripts.migrate import apply_migrations, MIGRATIONS_DIR
from scripts.projects import create_project
from scripts.roles import create_role


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_CLI = [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py")]


@pytest.fixture
def project(tmp_path):
    """Create a fresh DB + role + agent + project. Returns (db_path, project_id)."""
    db_path = tmp_path / "test.db"
    apply_migrations(str(db_path), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db_path), MIGRATIONS_DIR / "local-only")
    role = create_role(str(db_path), name="pm", description="PM")
    create_agent(str(db_path), id="agent-1", name="Test", role_id=role["id"], profile="")
    p = create_project(str(db_path), name="integration", description="d",
                       created_by="agent-1", repo="")
    return str(db_path), p["id"]


def _check_gate_cli(db_path, project_id, skill):
    result = subprocess.run(
        WORKFLOW_CLI + [db_path, "check-gate", str(project_id), skill],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, f"check-gate failed: {result.stderr}"
    return json.loads(result.stdout)


def _log_bypass_cli(db_path, project_id, skill, current, required, **kwargs):
    args = WORKFLOW_CLI + [db_path, "log-bypass", str(project_id), skill, current, required]
    if "agent_id" in kwargs:
        args += ["--agent", kwargs["agent_id"]]
    if "note" in kwargs:
        args += ["--note", kwargs["note"]]
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"})
    assert result.returncode == 0, f"log-bypass failed: {result.stderr}"
    return json.loads(result.stdout)


def test_full_bypass_flow(project):
    """Project at design:open; user invokes dev:plan; bypass; advance; clean state."""
    db_path, pid = project

    # 1. Agent calls check-gate for dev:plan while project is at design:open
    result = _check_gate_cli(db_path, pid, "dev:plan")
    assert result["allowed"] is False
    assert result["current_phase"] == "design:open"
    assert result["required_phase"] == "design:approved"

    # 2. User confirms bypass; agent calls log-bypass
    bypass = _log_bypass_cli(
        db_path, pid, "dev:plan",
        result["current_phase"], result["required_phase"],
        agent_id="agent-1", note="user explicitly approved out-of-phase plan work",
    )
    assert "bypass_id" in bypass and bypass["bypass_id"] > 0

    # 3. Bypass is recorded in phase_bypasses
    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT skill, current_phase, required_phase, agent_id, note "
            "FROM phase_bypasses WHERE id = ?", (bypass["bypass_id"],),
        ).fetchone()
    assert row == ("dev:plan", "design:open", "design:approved", "agent-1",
                   "user explicitly approved out-of-phase plan work")

    # 4. Agent later does explicit advancement
    workflow.advance_phase(db_path, pid, "design:approved")

    # 5. Subsequent check-gate for dev:plan now returns allowed=true
    result2 = _check_gate_cli(db_path, pid, "dev:plan")
    assert result2["allowed"] is True
    assert result2["current_phase"] == "design:approved"

    # 6. Bypass row remains (audit trail)
    with closing(get_connection(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?", (pid,),
        ).fetchone()[0]
    assert count == 1


def test_bypass_aggregates_for_retro(project):
    """Multiple bypasses across same key dedup; across different keys accumulate."""
    db_path, pid = project

    # Two bypasses of dev:plan from design:open within 60s -- dedup to one row
    _log_bypass_cli(db_path, pid, "dev:plan", "design:open", "design:approved")
    _log_bypass_cli(db_path, pid, "dev:plan", "design:open", "design:approved")

    # Advance to design:approved, then bypass dev:tdd
    workflow.advance_phase(db_path, pid, "design:approved")
    _log_bypass_cli(db_path, pid, "dev:tdd", "design:approved", "plan:approved")

    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            """SELECT skill, current_phase, required_phase, COUNT(*) AS n
               FROM phase_bypasses
               WHERE project_id = ?
               GROUP BY skill, current_phase, required_phase
               ORDER BY skill""",
            (pid,),
        ).fetchall()
    # Expect two grouped rows: dev:plan (n=1 due to dedup) and dev:tdd (n=1)
    assert rows == [
        ("dev:plan", "design:open", "design:approved", 1),
        ("dev:tdd", "design:approved", "plan:approved", 1),
    ]
