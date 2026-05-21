# tests/test_meetings.py
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.agents import create_agent
from scripts.meetings import (
    add_participant,
    create_meeting,
    delete_meeting,
    get_meeting,
    get_participants,
    search_meetings,
)
from scripts.migrate import apply_migrations
from scripts.roles import create_role

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _seed_workspace(db_path: str) -> int:
    """Seed a workspace row so v1.1.0 NOT NULL workspace_id FKs are satisfied."""
    now = "2026-05-18T00:00:00Z"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("myproj", "repo:myproj", "MyProj", "test workspace", now, now),
    )
    ws_id = cur.lastrowid
    conn.commit()
    conn.close()
    return ws_id


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Fake git workspace root with .ai/atelier.db migrated to v1.1.0.

    backend_local._workspace_root() resolves via find_git_root(), so we
    chdir into a directory containing a sentinel .git/. The db lives at
    <root>/.ai/atelier.db — both old (db_path-passing) and new
    (backend_local) code paths point at the same SQLite file.

    detect_mode() is pinned to "local" so the facade dispatches into
    backend_local even on dev machines that have Memex v2 installed."""
    from scripts import mode_detector

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    ws_id = _seed_workspace(str(db))
    return {"root": root, "db": str(db), "workspace_id": ws_id}


@pytest.fixture
def db_path(workspace):
    return workspace["db"]


@pytest.fixture
def workspace_id(workspace):
    return workspace["workspace_id"]


@pytest.fixture
def meetings_dir(workspace):
    d = workspace["root"] / ".ai" / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def agent_id(db_path):
    role = create_role(db_path, name="pm", description="PM")
    agent = create_agent(db_path, id="pm-1", name="PM", role_id=role["id"], profile="Expert PM")
    return agent["id"]


def test_create_meeting_writes_db_record(db_path, meetings_dir, agent_id, workspace_id):
    meeting = create_meeting(
        db_path,
        meetings_dir,
        title="Sprint Planning",
        date="2026-05-12",
        summary="Plan Q2 work",
        decisions="Ship auth by end of May",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    assert meeting["id"] == 1
    assert meeting["title"] == "Sprint Planning"
    assert meeting["filename"] == "2026-05-12-sprint-planning.md"


def test_create_meeting_writes_md_file(db_path, meetings_dir, agent_id, workspace_id):
    create_meeting(
        db_path,
        meetings_dir,
        title="Sprint Planning",
        date="2026-05-12",
        summary="Plan Q2 work",
        decisions="Ship auth by end of May",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    md_file = meetings_dir / "2026-05-12-sprint-planning.md"
    assert md_file.exists()
    content = md_file.read_text()
    assert "Sprint Planning" in content
    assert "Plan Q2 work" in content


def test_get_meeting(db_path, meetings_dir, agent_id, workspace_id):
    create_meeting(
        db_path,
        meetings_dir,
        title="Sprint Planning",
        date="2026-05-12",
        summary="Plan Q2",
        decisions="Ship",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    meeting = get_meeting(db_path, 1)
    assert meeting["title"] == "Sprint Planning"


def test_get_meeting_missing_returns_none(db_path):
    assert get_meeting(db_path, 999) is None


def test_add_and_get_participants(db_path, meetings_dir, agent_id, workspace_id):
    create_meeting(
        db_path,
        meetings_dir,
        title="Standup",
        date="2026-05-12",
        summary="Daily sync",
        decisions="",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    role = create_role(db_path, name="dev", description="Developer")
    create_agent(db_path, id="dev-1", name="Alice", role_id=role["id"], profile="Dev")
    add_participant(db_path, meeting_id=1, agent_id="pm-1")
    add_participant(db_path, meeting_id=1, agent_id="dev-1")
    participants = get_participants(db_path, 1)
    assert len(participants) == 2


def test_delete_meeting(db_path, meetings_dir, agent_id, workspace_id):
    create_meeting(
        db_path,
        meetings_dir,
        title="Sprint Planning",
        date="2026-05-12",
        summary="Plan",
        decisions="",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    assert delete_meeting(db_path, meetings_dir, 1) is True
    assert get_meeting(db_path, 1) is None
    assert not (meetings_dir / "2026-05-12-sprint-planning.md").exists()


def test_create_meeting_with_participants_preserves_block(
    db_path, meetings_dir, agent_id, workspace_id
):
    """Regression for T21 round-1 I1: backend_local.write_meeting was
    writing its own (participants-blind) renderer to the same .md
    path, clobbering the participants-aware one. After the fix,
    meetings.py owns the .md write and asks the backend to skip via
    skip_md=True — the Participants block must survive."""
    create_meeting(
        db_path,
        meetings_dir,
        title="Sprint Planning",
        date="2026-05-12",
        summary="Plan Q2 work",
        decisions="Ship auth by end of May",
        created_by=agent_id,
        participants=["pm-1", "dev-1"],
        workspace_id=workspace_id,
    )
    md_file = meetings_dir / "2026-05-12-sprint-planning.md"
    content = md_file.read_text()
    assert "## Participants" in content
    assert "- pm-1" in content
    assert "- dev-1" in content


def test_search_meetings(db_path, meetings_dir, agent_id, workspace_id):
    create_meeting(
        db_path,
        meetings_dir,
        title="Sprint Planning",
        date="2026-05-12",
        summary="Q2 roadmap",
        decisions="",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    create_meeting(
        db_path,
        meetings_dir,
        title="Standup",
        date="2026-05-12",
        summary="Daily sync",
        decisions="",
        created_by=agent_id,
        workspace_id=workspace_id,
    )
    results = search_meetings(db_path, query="roadmap")
    assert len(results) == 1
    assert results[0]["title"] == "Sprint Planning"


def test_create_meeting_cli_participants_writes_md_block(tmp_path, monkeypatch):
    """CLI --participants flag populates the .md Participants block; does not insert
    meeting_participants rows (by design — use add-participant for DB linkage)."""
    REPO_ROOT = Path(__file__).parent.parent
    MIGRATIONS_DIR = REPO_ROOT / "migrations"

    # Set up a fake git workspace with a fully migrated DB.
    clone_dir = tmp_path / "myproj"
    clone_dir.mkdir()
    (clone_dir / ".git").mkdir()
    db = clone_dir / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")

    # Seed a workspace row so the NOT NULL workspace_id FK is satisfied.
    now = "2026-05-21T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("myproj", "repo:myproj", "MyProj", "test workspace", now, now),
    )
    conn.commit()
    conn.close()

    meetings_dir = clone_dir / ".ai" / "meetings"
    meetings_dir.mkdir(parents=True, exist_ok=True)

    # Retrieve the seeded workspace_id so we can pass --workspace-id to the CLI.
    conn2 = sqlite3.connect(str(db))
    ws_id = conn2.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]
    conn2.close()

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "meetings.py"),
            "create",
            "Test Meeting",
            "2026-05-21",
            "A summary",
            "A decision",
            "tester",
            "--meetings-dir",
            str(meetings_dir),
            "--workspace-id",
            str(ws_id),
            "--participants",
            "pm-1,dev-1",
        ],
        cwd=str(clone_dir),
        capture_output=True,
        text=True,
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": __import__("os").environ.get("PATH", ""),
            # Force local mode by pointing HOME at the (empty) tmp clone dir so
            # mode_detector._memex_plugin_reachable() sees no ~/.memex/config.json.
            "HOME": str(clone_dir),
        },
    )
    assert result.returncode == 0, (
        f"CLI exited non-zero:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # The .md file must carry the Participants block with both agent IDs.
    md_file = meetings_dir / "2026-05-21-test-meeting.md"
    assert md_file.exists(), "meeting .md file was not written"
    content = md_file.read_text(encoding="utf-8")
    assert "## Participants" in content, "Participants heading missing from .md"
    assert "pm-1" in content, "pm-1 missing from .md Participants block"
    assert "dev-1" in content, "dev-1 missing from .md Participants block"

    # By design: CLI create does NOT insert rows into meeting_participants.
    # Use `add-participant` for DB linkage.
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT * FROM meeting_participants").fetchall()
    conn.close()
    assert rows == [], (
        f"meeting_participants should be empty after CLI create (got {rows}); "
        "use add-participant for DB linkage"
    )
