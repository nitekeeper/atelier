"""Tests for scripts/worktree.py — merge-back flow."""
import subprocess
from pathlib import Path

import pytest


# ── Git helpers ───────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=True,
        capture_output=True, text=True, encoding="utf-8",
    )


def _git_no_check(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=False,
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture
def main_repo(tmp_path):
    """Repo with one commit on main, configured for test use."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@test.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("# Test\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


@pytest.fixture
def repo_with_worktree(tmp_path, main_repo):
    """Main repo plus a linked worktree on branch 'claude/wt-1'."""
    wt_path = tmp_path / "worktree"
    _git(["worktree", "add", "-b", "claude/wt-1", str(wt_path)], main_repo)
    return main_repo, wt_path


# ── detect_worktree ───────────────────────────────────────────────────────

class TestDetectWorktree:
    def test_main_repo_is_not_a_worktree(self, main_repo):
        from scripts.worktree import detect_worktree
        is_wt, git_dir = detect_worktree(main_repo)
        assert is_wt is False
        assert "worktrees" not in git_dir.replace("\\", "/")

    def test_linked_worktree_is_detected(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        from scripts.worktree import detect_worktree
        is_wt, git_dir = detect_worktree(wt_path)
        assert is_wt is True
        assert "worktrees" in git_dir.replace("\\", "/")


# ── get_current_branch ────────────────────────────────────────────────────

class TestGetCurrentBranch:
    def test_returns_branch_name_in_main_repo(self, main_repo):
        from scripts.worktree import get_current_branch
        branch = get_current_branch(main_repo)
        assert branch == "main"

    def test_returns_worktree_branch_name(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        from scripts.worktree import get_current_branch
        branch = get_current_branch(wt_path)
        assert branch == "claude/wt-1"


# ── parse_main_worktree ───────────────────────────────────────────────────

class TestParseMainWorktree:
    def test_returns_main_repo_path_and_branch(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        from scripts.worktree import parse_main_worktree
        path, branch = parse_main_worktree(wt_path)
        # Normalize separators for comparison
        assert Path(path).resolve() == main_repo.resolve()
        assert branch == "main"

    def test_from_main_repo_returns_itself(self, main_repo):
        from scripts.worktree import parse_main_worktree
        path, branch = parse_main_worktree(main_repo)
        assert Path(path).resolve() == main_repo.resolve()
        assert branch == "main"


# ── merge_back ────────────────────────────────────────────────────────────

class TestMergeBack:
    def test_clean_merge_succeeds(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        # Add a commit on the worktree branch
        (wt_path / "feature.txt").write_text("feature work\n")
        _git(["add", "."], wt_path)
        _git(["commit", "-m", "add feature"], wt_path)

        from scripts.worktree import merge_back
        merge_back(wt_path)

        # Branch merged into main
        log = _git(["log", "--oneline"], main_repo)
        assert "Merge claude/wt-1 into main" in log.stdout

    def test_worktree_removed_after_merge(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        (wt_path / "f.txt").write_text("x")
        _git(["add", "."], wt_path)
        _git(["commit", "-m", "add f"], wt_path)

        from scripts.worktree import merge_back
        merge_back(wt_path)

        wt_list = _git_no_check(["worktree", "list"], main_repo)
        assert str(wt_path) not in wt_list.stdout

    def test_branch_deleted_after_merge(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        (wt_path / "g.txt").write_text("y")
        _git(["add", "."], wt_path)
        _git(["commit", "-m", "add g"], wt_path)

        from scripts.worktree import merge_back
        merge_back(wt_path)

        branches = _git(["branch"], main_repo)
        assert "claude/wt-1" not in branches.stdout

    def test_no_changes_still_merges_and_cleans(self, repo_with_worktree):
        """Worktree with no new commits should still be cleaned up."""
        main_repo, wt_path = repo_with_worktree

        from scripts.worktree import merge_back
        merge_back(wt_path)

        # Worktree should be gone
        wt_list = _git_no_check(["worktree", "list"], main_repo)
        assert str(wt_path) not in wt_list.stdout

    def test_uncommitted_changes_are_committed_first(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        # Leave file uncommitted
        (wt_path / "dirty.txt").write_text("uncommitted\n")

        from scripts.worktree import merge_back
        merge_back(wt_path)

        # dirty.txt should appear in main's history after merge
        show = _git(["show", "--name-only", "HEAD"], main_repo)
        # The merge commit or the preceding commit should contain dirty.txt
        log = _git(["log", "--all", "--name-only", "--pretty=format:"], main_repo)
        assert "dirty.txt" in log.stdout

    def test_not_a_worktree_exits_cleanly(self, main_repo):
        from scripts.worktree import merge_back
        # Should print a message and return without error
        merge_back(main_repo)  # must not raise

    def test_dirty_main_aborts(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        # Dirty up the main workspace
        (main_repo / "dirty_main.txt").write_text("oops\n")

        from scripts.worktree import merge_back
        with pytest.raises(SystemExit) as exc_info:
            merge_back(wt_path)
        assert exc_info.value.code == 1

    def test_conflict_aborts_and_leaves_worktree_intact(self, repo_with_worktree):
        main_repo, wt_path = repo_with_worktree
        # Create conflicting change on main
        (main_repo / "conflict.txt").write_text("main version\n")
        _git(["add", "."], main_repo)
        _git(["commit", "-m", "main adds conflict.txt"], main_repo)
        # Create conflicting change on worktree
        (wt_path / "conflict.txt").write_text("worktree version\n")
        _git(["add", "."], wt_path)
        _git(["commit", "-m", "wt adds conflict.txt"], wt_path)

        from scripts.worktree import merge_back
        with pytest.raises(SystemExit) as exc_info:
            merge_back(wt_path)
        assert exc_info.value.code == 1

        # Worktree must still exist after conflict abort (normalize separators for Windows)
        wt_list = _git(["worktree", "list"], main_repo)
        assert wt_path.as_posix() in wt_list.stdout.replace("\\", "/")

        # Main must be in a clean state (merge aborted)
        status = _git(["status", "--porcelain"], main_repo)
        assert "conflict.txt" not in status.stdout


# ── CLI ───────────────────────────────────────────────────────────────────

class TestCLI:
    def test_unknown_command_exits_1(self):
        result = subprocess.run(
            ["python", "scripts/worktree.py", "bogus"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1

    def test_no_command_exits_1(self):
        result = subprocess.run(
            ["python", "scripts/worktree.py"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1
