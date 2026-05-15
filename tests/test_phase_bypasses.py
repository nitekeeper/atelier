"""Tests for the phase_bypasses table and the log-bypass workflow command."""
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts.db import get_connection
from scripts.migrate import apply_migrations, MIGRATIONS_DIR
from scripts.roles import create_role
from scripts.agents import create_agent
from scripts.projects import create_project
from scripts import workflow


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path):
    """Migrate a fresh DB, create role+agent+project; returns (db_path_str, project_id)."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    role = create_role(db_path, name="pm", description="PM")
    create_agent(db_path, id="test-agent", name="Test Agent", role_id=role["id"], profile="Tester")
    proj = create_project(db_path, name="test", description=None, created_by="test-agent")
    return db_path, proj["id"]


def test_log_bypass_writes_row(project):
    """log_bypass inserts a row and returns a positive integer id."""
    db_path, pid = project
    bypass_id = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
        agent_id="test-agent", note="testing soft wall bypass",
    )
    assert isinstance(bypass_id, int) and bypass_id > 0

    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT project_id, skill, current_phase, required_phase, agent_id, note "
            "FROM phase_bypasses WHERE id = ?", (bypass_id,)
        ).fetchone()
    assert row == (pid, "dev:plan", "design:open", "design:approved",
                   "test-agent", "testing soft wall bypass")


def test_log_bypass_idempotent_within_one_minute(project):
    """Same (project, skill, current, required) inside 60 seconds — only one row."""
    db_path, pid = project
    first = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    second = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    # Idempotency returns the existing row id rather than creating a new one
    assert first == second

    with closing(get_connection(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?", (pid,)
        ).fetchone()[0]
    assert count == 1


def test_log_bypass_cli_writes_row(project):
    """CLI log-bypass subcommand inserts a row and exits with code 0."""
    db_path, pid = project
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py"),
         db_path, "log-bypass", str(pid), "dev:plan",
         "design:open", "design:approved",
         "--agent", "test-agent", "--note", "from CLI"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    output = json.loads(result.stdout)
    bypass_id = output["bypass_id"]
    assert isinstance(bypass_id, int) and bypass_id > 0

    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT skill, agent_id, note FROM phase_bypasses WHERE id = ?",
            (bypass_id,),
        ).fetchone()
    assert row == ("dev:plan", "test-agent", "from CLI")


def test_log_bypass_with_none_defaults_stores_null(project):
    """When agent_id and note are omitted, the row stores SQL NULL for both."""
    db_path, pid = project
    bypass_id = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT agent_id, note FROM phase_bypasses WHERE id = ?", (bypass_id,)
        ).fetchone()
    assert row[0] is None, "agent_id should be SQL NULL"
    assert row[1] is None, "note should be SQL NULL"


def test_log_bypass_creates_new_row_after_idempotency_window(project):
    """Two bypasses separated by >60s produce distinct rows (idempotency window expired)."""
    db_path, pid = project
    first = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    # Backdate the first row past the 60-second window
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "UPDATE phase_bypasses SET bypassed_at = datetime('now', '-61 seconds') WHERE id = ?",
            (first,),
        )
        conn.commit()
    second = workflow.log_bypass(
        db_path, pid, skill="dev:plan",
        current_phase="design:open", required_phase="design:approved",
    )
    assert second != first, "after window expiry, should create new row"
    with closing(get_connection(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_bypasses WHERE project_id = ?", (pid,)
        ).fetchone()[0]
    assert count == 2


def test_log_bypass_cli_missing_flag_value(project):
    """--agent without a value exits 1 and prints error to stderr."""
    db_path, pid = project
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py"),
         db_path, "log-bypass", str(pid), "dev:plan",
         "design:open", "design:approved",
         "--agent"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 1
    assert "requires a value" in result.stderr
