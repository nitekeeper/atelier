"""Tests for scripts/self_improve.py git operations."""
import subprocess
from pathlib import Path

import pytest


# ── Git fixtures ───────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, encoding="utf-8")


@pytest.fixture
def bare_remote(tmp_path):
    """Bare repo acting as origin."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return remote


@pytest.fixture
def source_repo(tmp_path, bare_remote):
    """Local repo with one passing test, pushed to bare_remote."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@test.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["remote", "add", "origin", str(bare_remote)], repo)
    (repo / "README.md").write_text("# Atelier\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_dummy.py").write_text("def test_ok(): assert True\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    _git(["push", "-u", "origin", "main"], repo)
    return repo


# ── clone_repo ────────────────────────────────────────────────────────────

class TestCloneRepo:
    def test_clone_creates_directory_with_contents(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        assert dest.exists()
        assert (dest / "README.md").exists()

    def test_clone_sets_git_identity(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "self-improve@atelier.local"


# ── create_branch ─────────────────────────────────────────────────────────

class TestCreateBranch:
    def test_branch_name_starts_with_prefix(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 3)
        assert branch.startswith("self-improve/cycle-3-")

    def test_branch_is_checked_out(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 1)
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == branch


# ── run_tests_in_clone ────────────────────────────────────────────────────

class TestRunTestsInClone:
    def test_passing_tests_returns_true_and_count(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, run_tests_in_clone
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        passed, count = run_tests_in_clone(dest)
        assert passed is True
        assert count == 1

    def test_failing_tests_returns_false(self, tmp_path, bare_remote, source_repo):
        # Push a failing test to the remote
        (source_repo / "tests" / "test_fail.py").write_text("def test_fail(): assert False\n")
        _git(["add", "."], source_repo)
        _git(["commit", "-m", "add failing test"], source_repo)
        _git(["push"], source_repo)
        from scripts.self_improve import clone_repo, run_tests_in_clone
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        passed, _ = run_tests_in_clone(dest)
        assert passed is False


# ── write_minutes ─────────────────────────────────────────────────────────

class TestWriteMinutes:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        from scripts.self_improve import write_minutes
        path = tmp_path / "docs" / "self-improve" / "cycle-1-minutes.md"
        content = "# Meeting\n\n## Agenda\n1. Improve things"
        write_minutes(path, content)
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content

    def test_overwrites_existing_file(self, tmp_path):
        from scripts.self_improve import write_minutes
        path = tmp_path / "minutes.md"
        path.write_text("old content")
        write_minutes(path, "new content")
        assert path.read_text(encoding="utf-8") == "new content"


# ── commit_cycle ──────────────────────────────────────────────────────────

class TestCommitCycle:
    def test_commit_message_contains_required_fields(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, commit_cycle
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        create_branch(dest, 2)
        (dest / "CHANGES.txt").write_text("a change")
        commit_cycle(
            clone_dir=dest,
            cycle_n=2,
            decisions=["Improve error handling in workflow.py", "Add retry logic"],
            participants=["Dr. Priya Nair", "Dr. Nadia Petrov"],
            n_tests=7,
            subject="improve error handling",
            minutes_rel_path="docs/self-improve/2026-05-14-cycle-2-minutes.md",
        )
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=dest, capture_output=True, text=True,
        )
        msg = result.stdout
        assert "self-improve(cycle-2):" in msg
        assert "Decisions:" in msg
        assert "1. Improve error handling in workflow.py" in msg
        assert "Tests: 7 passed" in msg
        assert "Subject: improve error handling" in msg
        assert "Dr. Priya Nair" in msg

    def test_commit_stages_all_changes(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, commit_cycle
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        create_branch(dest, 1)
        (dest / "new_file.txt").write_text("new")
        commit_cycle(
            clone_dir=dest, cycle_n=1,
            decisions=["Add file"], participants=["Dr. Test"],
            n_tests=1, subject="test",
            minutes_rel_path="docs/self-improve/minutes.md",
        )
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:"],
            cwd=dest, capture_output=True, text=True,
        )
        assert "new_file.txt" in result.stdout


# ── cleanup_experiment ────────────────────────────────────────────────────

class TestCleanupExperiment:
    def test_removes_directory_recursively(self, tmp_path):
        from scripts.self_improve import cleanup_experiment
        exp = tmp_path / "experiment"
        (exp / "atelier").mkdir(parents=True)
        (exp / "atelier" / "file.txt").write_text("x")
        cleanup_experiment(exp)
        assert not exp.exists()

    def test_no_error_if_already_absent(self, tmp_path):
        from scripts.self_improve import cleanup_experiment
        cleanup_experiment(tmp_path / "nonexistent")  # must not raise
