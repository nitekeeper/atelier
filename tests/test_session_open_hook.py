"""Tests for hooks/session_open.py"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add hooks dir to path for direct import
HOOKS_DIR = Path(__file__).parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import session_open  # noqa: E402


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
