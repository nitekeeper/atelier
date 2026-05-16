"""Tests for hooks/session_open.py"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add hooks dir to path for direct import
HOOKS_DIR = Path(__file__).parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import session_open  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "session_open.py"


class TestFindActiveProject:
    def test_no_ai_dir(self, tmp_path):
        """No .ai/ directory → None."""
        assert session_open.find_active_project(tmp_path) is None

    def test_no_active_project_file(self, tmp_path):
        """Directory exists but no active_project file → None."""
        (tmp_path / ".ai").mkdir()
        assert session_open.find_active_project(tmp_path) is None

    def test_empty_file(self, tmp_path):
        """Empty file → None."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("")
        assert session_open.find_active_project(tmp_path) is None

    def test_whitespace_only(self, tmp_path):
        """Whitespace-only file → None."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("  \n  ")
        assert session_open.find_active_project(tmp_path) is None

    def test_valid_integer_id(self, tmp_path):
        """Valid project id → returned as string."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("42\n")
        assert session_open.find_active_project(tmp_path) == "42"

    def test_strips_whitespace(self, tmp_path):
        """ID with surrounding whitespace → stripped."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("  7  \n")
        assert session_open.find_active_project(tmp_path) == "7"


class TestBuildAnnouncement:
    def test_no_session(self):
        """No prior session → informational message."""
        msg = session_open.build_announcement("5", None)
        assert "Project 5" in msg
        assert "no prior session" in msg

    def test_session_with_phase_only(self):
        """Session with phase and no extras → phase announced."""
        session = {
            "phase": "tdd:green",
            "pm_notes": None,
            "next_action": None,
            "status": "in-progress",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("3", session)
        assert "tdd:green" in msg
        assert "Project 3" in msg

    def test_session_with_all_fields(self):
        """Session with all fields → all announced."""
        session = {
            "phase": "review:open",
            "pm_notes": "PR needs rebase before re-review",
            "next_action": "Run dev:review for project 3",
            "status": "in-progress",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("3", session)
        assert "review:open" in msg
        assert "PR needs rebase before re-review" in msg
        assert "Run dev:review for project 3" in msg

    def test_blocked_session_with_reason(self):
        """Blocked session with reason → BLOCKED label and reason."""
        session = {
            "phase": "tdd:red",
            "pm_notes": None,
            "next_action": None,
            "status": "blocked",
            "blocking_reason": "Missing test data fixtures",
        }
        msg = session_open.build_announcement("7", session)
        assert "BLOCKED" in msg
        assert "Missing test data fixtures" in msg

    def test_blocked_without_reason(self):
        """Blocked status with no reason → no BLOCKED label (reason unknown)."""
        session = {
            "phase": "tdd:red",
            "pm_notes": None,
            "next_action": None,
            "status": "blocked",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("7", session)
        assert "BLOCKED" not in msg

    def test_missing_phase_field(self):
        """Session with missing phase → graceful fallback."""
        session = {
            "pm_notes": None,
            "next_action": None,
            "status": "in-progress",
            "blocking_reason": None,
        }
        msg = session_open.build_announcement("1", session)
        assert "Project 1" in msg
        assert "unknown" in msg


class TestFetchLatestSession:
    def test_subprocess_exception(self):
        """subprocess.run raises → error string returned."""
        with patch("session_open.subprocess.run", side_effect=OSError("timeout")):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, str)
        assert result.startswith("error:")
        assert "timeout" in result

    def test_nonzero_returncode(self):
        """session.py exits non-zero → error string returned."""
        mock = MagicMock()
        mock.returncode = 1
        mock.stdout = ""
        mock.stderr = "no such table: sessions"
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, str)
        assert result.startswith("error:")

    def test_empty_stdout(self):
        """session.py returns 0 but empty stdout → None (no prior session)."""
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert result is None

    def test_valid_json_returned(self):
        """session.py returns valid JSON → parsed dict."""
        session_data = {
            "id": 1,
            "phase": "tdd:green",
            "pm_notes": "on track",
            "next_action": "Run dev:review",
            "status": "in-progress",
            "blocking_reason": None,
        }
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = json.dumps(session_data)
        mock.stderr = ""
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, dict)
        assert result["phase"] == "tdd:green"

    def test_invalid_json(self):
        """session.py returns non-JSON → error string."""
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "not json at all"
        mock.stderr = ""
        with patch("session_open.subprocess.run", return_value=mock):
            result = session_open.fetch_latest_session(Path("/fake"), "1")
        assert isinstance(result, str)
        assert result.startswith("error:")


class TestMain:
    def test_no_active_project_exits_silently(self, tmp_path, capsys):
        """No .ai/active_project → exits silently (SystemExit 0), no output."""
        with patch("session_open.Path.cwd", return_value=tmp_path):
            with pytest.raises(SystemExit) as exc_info:
                session_open.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_announce_on_first_call(self, tmp_path, capsys):
        """First call with valid project → prints announcement."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("1")
        session_data = {
            "phase": "tdd:clean",
            "pm_notes": None,
            "next_action": "Run dev:review",
            "status": "in-progress",
            "blocking_reason": None,
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(session_data)
        mock_result.stderr = ""
        with patch("session_open.Path.cwd", return_value=tmp_path), \
             patch("session_open.subprocess.run", return_value=mock_result):
            session_open.main()
        captured = capsys.readouterr()
        assert "tdd:clean" in captured.out
        assert (tmp_path / ".atelier-session-announced").exists()

    def test_flag_suppresses_second_call(self, tmp_path, capsys):
        """If flag file exists, second call produces no output (SystemExit 0)."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("1")
        (tmp_path / ".atelier-session-announced").write_text("announced")
        with patch("session_open.Path.cwd", return_value=tmp_path):
            with pytest.raises(SystemExit) as exc_info:
                session_open.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_db_error_prints_warning_not_raises(self, tmp_path, capsys):
        """DB error → warning printed, no exception raised, no sys.exit(1)."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("1")
        with patch("session_open.Path.cwd", return_value=tmp_path), \
             patch("session_open.subprocess.run", side_effect=OSError("DB gone")):
            session_open.main()  # Must not raise
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower()
        assert "DB gone" in captured.out

    def test_main_no_guidance_when_phase_missing_from_dict(self, tmp_path, capsys, monkeypatch):
        """When session dict lacks 'phase' key, no guidance is appended (no crash)."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("1")
        monkeypatch.setattr(session_open, "find_active_project", lambda cwd: "1")
        monkeypatch.setattr(
            session_open, "fetch_latest_session",
            lambda scripts_dir, project_id: {"notes": "x"},  # no 'phase' key
        )
        monkeypatch.setattr(
            session_open, "build_announcement",
            lambda project_id, session: "Session at unknown phase.",
        )
        with patch("session_open.Path.cwd", return_value=tmp_path):
            session_open.main()
        captured = capsys.readouterr()
        # Should NOT raise, and should NOT contain "Recommended next action"
        assert "Recommended next action" not in captured.out


class TestGetPhaseGuidance:
    def test_known_phase_returns_string(self, tmp_path, monkeypatch):
        """Returns a formatted guidance line for a known phase using an isolated SKILL.md."""
        skill_path = tmp_path / "SKILL.md"
        skill_path.write_text(
            "---\nname: test\n---\n## Phase guidance\n\n"
            "| Phase | Recommended next action | Skill |\n"
            "|---|---|---|\n"
            "| `design:open` | Continue grilling | `dev:design` |\n\n"
            "## Other section\nfoo\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(session_open, "_RUN_PATH", skill_path)
        result = session_open.get_phase_guidance("design:open")
        assert result is not None
        assert "Continue grilling" in result
        assert "dev:design" in result

    def test_unknown_phase_returns_none(self, tmp_path, monkeypatch):
        """Returns None for a phase not present in the table."""
        skill_path = tmp_path / "SKILL.md"
        skill_path.write_text(
            "---\nname: test\n---\n## Phase guidance\n\n"
            "| Phase | Recommended next action | Skill |\n"
            "|---|---|---|\n"
            "| `design:open` | foo | `bar` |\n\n## Other\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(session_open, "_RUN_PATH", skill_path)
        assert session_open.get_phase_guidance("nonexistent:phase") is None

    def test_missing_skill_file_returns_none(self, tmp_path, monkeypatch):
        """If SKILL.md is missing, returns None without raising."""
        monkeypatch.setattr(session_open, "_RUN_PATH", tmp_path / "no_such_file.md")
        result = session_open.get_phase_guidance("design:open")
        assert result is None

    def test_malformed_skill_file_returns_none(self, tmp_path, monkeypatch):
        """If SKILL.md has no Phase guidance section, returns None."""
        fake = tmp_path / "SKILL.md"
        fake.write_text("# No phase guidance here\n", encoding="utf-8")
        monkeypatch.setattr(session_open, "_RUN_PATH", fake)
        result = session_open.get_phase_guidance("design:open")
        assert result is None

    def test_main_appends_guidance_after_phase_announcement(self, tmp_path, capsys):
        """main() prints guidance line when session has a known phase."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("1")
        session_data = {
            "phase": "design:open",
            "pm_notes": None,
            "next_action": None,
            "status": "in-progress",
            "blocking_reason": None,
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(session_data)
        mock_result.stderr = ""
        with patch("session_open.Path.cwd", return_value=tmp_path), \
             patch("session_open.subprocess.run", return_value=mock_result):
            session_open.main()
        captured = capsys.readouterr()
        assert "design:open" in captured.out
        assert "Recommended next action:" in captured.out
        assert "internal/dev-design/SKILL.md" in captured.out

    def test_main_no_guidance_when_result_is_none(self, tmp_path, capsys):
        """main() does not print guidance when there is no prior session (result is None)."""
        (tmp_path / ".ai").mkdir()
        (tmp_path / ".ai" / "active_project").write_text("1")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("session_open.Path.cwd", return_value=tmp_path), \
             patch("session_open.subprocess.run", return_value=mock_result):
            session_open.main()
        captured = capsys.readouterr()
        assert "Recommended next action:" not in captured.out


# ---------------------------------------------------------------------------
# Subprocess integration tests — run the hook as a real process with a live DB
# ---------------------------------------------------------------------------

def _make_project_at_phase(db_path: str, phase: str) -> str:
    """Set up a project at the given phase and write a session row. Returns project_id as string.

    Walks the valid transition graph to reach the target phase without
    using force_phase (which does not exist). The DB must already have
    migrations applied before calling this.
    """
    from scripts.migrate import apply_migrations, MIGRATIONS_DIR
    from scripts.projects import create_project
    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.session import write_session
    from scripts import workflow

    apply_migrations(db_path, MIGRATIONS_DIR)
    role = create_role(db_path, "pm", "Project Manager")
    create_agent(db_path, "test-agent", "Test Agent", role["id"], "")
    project = create_project(
        db_path, name="t", description="d", created_by="test-agent"
    )
    pid = project["id"]

    # Walk valid transitions to reach the target phase.
    _TRANSITION_PATH = [
        "design:open",
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
    ]
    if phase not in _TRANSITION_PATH:
        raise ValueError(f"Phase '{phase}' not in known transition path")
    target_idx = _TRANSITION_PATH.index(phase)
    for next_phase in _TRANSITION_PATH[1:target_idx + 1]:
        workflow.advance_phase(db_path, pid, next_phase)

    # Write a session row so read-latest returns JSON (not the "No session found" message).
    write_session(db_path, pid, "test-agent", phase, "in-progress")

    return str(pid)


@pytest.fixture
def project_at_phase(tmp_path):
    """Factory fixture: returns a callable that sets up a project at a given phase.

    Returns (db_path_str, project_id_str, working_dir).
    The working_dir contains .ai/memex.db and .ai/active_project as
    session.py expects.
    """
    def _make(phase: str):
        ai_dir = tmp_path / ".ai"
        ai_dir.mkdir(exist_ok=True)
        db_path = str(ai_dir / "memex.db")
        pid = _make_project_at_phase(db_path, phase)
        (ai_dir / "active_project").write_text(pid, encoding="utf-8")
        return db_path, pid, tmp_path
    return _make


_HOOK_ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"}


@pytest.mark.parametrize("phase,expected_skill", [
    ("design:open", "internal/dev-design/SKILL.md"),
    ("plan:approved", "internal/dev-tdd/SKILL.md"),
    ("tdd:clean", "internal/dev-review/SKILL.md"),
    ("review:approved", "internal/dev-security/SKILL.md"),
    ("qa:approved", "internal/dev-finish/SKILL.md"),
])
def test_hook_appends_phase_guidance(project_at_phase, phase, expected_skill):
    """For each phase, the hook output mentions the phase and the recommended skill."""
    db_path, pid, cwd = project_at_phase(phase)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(cwd),
        env=_HOOK_ENV,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert phase in result.stdout, f"phase '{phase}' not in output: {result.stdout!r}"
    assert expected_skill in result.stdout, (
        f"skill '{expected_skill}' not in output: {result.stdout!r}"
    )


def test_hook_handles_missing_using_atelier_gracefully(project_at_phase, tmp_path):
    """If run/SKILL.md is missing, hook still announces phase."""
    import shutil
    db_path, pid, cwd = project_at_phase("design:open")

    # Create an isolated directory tree in tmp_path with the hook and scripts
    # copied, but NO skills/run/SKILL.md — that's the test condition.
    (tmp_path / "hooks").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    shutil.copytree(str(REPO_ROOT / "scripts"), str(tmp_path / "scripts"))
    target_hook = tmp_path / "hooks" / "session_open.py"
    target_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(target_hook)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0
    assert "design:open" in result.stdout
