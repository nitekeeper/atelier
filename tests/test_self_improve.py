"""Tests for scripts/self_improve.py git operations."""
import os
import stat
import subprocess
import sys
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


# ── get_remote_url ────────────────────────────────────────────────────────

class TestGetRemoteUrl:
    def test_returns_origin_url(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, get_remote_url
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        url = get_remote_url(dest)
        assert str(bare_remote) in url


# ── push_branch ───────────────────────────────────────────────────────────

class TestPushBranch:
    def test_branch_appears_on_remote_after_push(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, push_branch
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 5)
        push_branch(dest, branch)
        result = subprocess.run(
            ["git", "ls-remote", "--heads", str(bare_remote)],
            capture_output=True, text=True,
        )
        assert branch in result.stdout


# ── pull_main ─────────────────────────────────────────────────────────────

class TestPullMain:
    def test_pulls_new_commit_from_remote(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, pull_main
        # Make a new commit in source_repo and push
        (source_repo / "extra.txt").write_text("extra")
        _git(["add", "."], source_repo)
        _git(["commit", "-m", "extra commit"], source_repo)
        _git(["push"], source_repo)
        # Clone fresh, then call pull_main to pick up the new commit
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        # Verify extra.txt is present after clone (it was pushed)
        assert (dest / "extra.txt").exists()
        # Now verify pull_main doesn't raise on an already-up-to-date repo
        pull_main(dest)  # must not raise


# ── auto_merge_to_main ────────────────────────────────────────────────────

class TestAutoMergeToMain:
    def test_merges_with_dirty_main_stashes_and_restores(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, push_branch, auto_merge_to_main, commit_cycle
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 9)
        (dest / "improvement.txt").write_text("improvement")
        commit_cycle(
            clone_dir=dest, cycle_n=9,
            decisions=["Add improvement"], participants=["Dr. Test"],
            n_tests=1, subject="test",
            minutes_rel_path="docs/self-improve/minutes.md",
        )
        push_branch(dest, branch)
        # Dirty up the main workspace with an uncommitted file
        (source_repo / "wip.txt").write_text("work in progress")
        auto_merge_to_main(source_repo, branch)
        # Stashed change must be restored
        assert (source_repo / "wip.txt").exists()
        assert (source_repo / "wip.txt").read_text() == "work in progress"

    def test_merges_branch_into_main_in_source_repo(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch, push_branch, auto_merge_to_main
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        branch = create_branch(dest, 1)
        (dest / "improvement.txt").write_text("improvement")
        from scripts.self_improve import commit_cycle
        commit_cycle(
            clone_dir=dest, cycle_n=1,
            decisions=["Add improvement"], participants=["Dr. Test"],
            n_tests=1, subject="test",
            minutes_rel_path="docs/self-improve/minutes.md",
        )
        push_branch(dest, branch)
        auto_merge_to_main(source_repo, branch)
        result = subprocess.run(
            ["git", "log", "--oneline", "-2"],
            cwd=source_repo, capture_output=True, text=True,
        )
        assert "Merge" in result.stdout


# ── sync_worktree_with_main ───────────────────────────────────────────────

@pytest.fixture
def worktree_behind_main(tmp_path, bare_remote, source_repo):
    """A linked worktree on a feature branch that is 1 commit behind main.

    Returns (main_repo, worktree_path). Calling sync_worktree_with_main(worktree)
    on a clean worktree must fast-forward it to main's HEAD.
    """
    wt_path = tmp_path / "worktree"
    _git(["worktree", "add", "-b", "claude/wt-test", str(wt_path)], source_repo)
    # Advance main by one commit, leaving the worktree branch behind
    (source_repo / "advance.txt").write_text("main has moved\n")
    _git(["add", "."], source_repo)
    _git(["commit", "-m", "advance main"], source_repo)
    return source_repo, wt_path


class TestSyncWorktreeWithMain:
    def test_clean_worktree_fast_forwards(self, worktree_behind_main):
        from scripts.self_improve import sync_worktree_with_main
        _main, wt = worktree_behind_main
        msg = sync_worktree_with_main(wt)
        assert "fast-forwarded to main" in msg
        assert (wt / "advance.txt").exists()

    def test_untracked_claude_only_still_fast_forwards(self, worktree_behind_main):
        from scripts.self_improve import sync_worktree_with_main
        _main, wt = worktree_behind_main
        (wt / ".claude").mkdir()
        (wt / ".claude" / "session.json").write_text("{}")
        msg = sync_worktree_with_main(wt)
        assert "fast-forwarded to main" in msg
        assert "Warning" not in msg  # .claude/ is silently ignored
        assert (wt / "advance.txt").exists()

    def test_untracked_other_warns_but_still_fast_forwards(self, worktree_behind_main):
        from scripts.self_improve import sync_worktree_with_main
        _main, wt = worktree_behind_main
        (wt / "scratch.tmp").write_text("local note")
        msg = sync_worktree_with_main(wt)
        assert "Warning" in msg
        assert "fast-forwarded to main" in msg
        assert (wt / "advance.txt").exists()
        # Untracked file is preserved
        assert (wt / "scratch.tmp").read_text() == "local note"

    def test_tracked_dirty_skips_sync(self, worktree_behind_main):
        from scripts.self_improve import sync_worktree_with_main
        _main, wt = worktree_behind_main
        # Modify a tracked file
        (wt / "README.md").write_text("# Atelier (locally modified)\n")
        msg = sync_worktree_with_main(wt)
        assert "skipping sync" in msg
        assert "uncommitted tracked changes" in msg
        # main's new file must NOT have arrived
        assert not (wt / "advance.txt").exists()


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

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only readonly attribute bug")
    def test_cleanup_removes_readonly_files_on_windows(self, tmp_path):
        """Read-only files (like git pack objects) must not block cleanup on Windows."""
        from scripts.self_improve import cleanup_experiment
        exp = tmp_path / "experiment"
        (exp / "sub").mkdir(parents=True)
        readonly_file = exp / "sub" / "readonly.txt"
        readonly_file.write_text("locked")
        os.chmod(readonly_file, stat.S_IREAD)

        cleanup_experiment(exp)
        assert not exp.exists()


# ── CLI ───────────────────────────────────────────────────────────────────

# ── repo_dir resolution from worktree ────────────────────────────────────

class TestRepoDir:
    def test_resolves_main_repo_when_invoked_from_worktree(self, tmp_path, bare_remote, source_repo):
        """CLI repo_dir must point to main workspace even when run from a linked worktree."""
        wt_path = tmp_path / "worktree"
        _git(["worktree", "add", "-b", "feat/test", str(wt_path)], source_repo)
        result = subprocess.run(
            ["python", str(Path.cwd() / "scripts" / "self_improve.py"),
             "pull"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(wt_path),
            env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
        )
        # pull from a worktree context must not crash with checkout-main errors
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


class TestCLI:
    def test_unknown_command_exits_1(self):
        result = subprocess.run(
            ["python", "scripts/self_improve.py", "bogus"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1

    def test_no_command_exits_1(self):
        result = subprocess.run(
            ["python", "scripts/self_improve.py"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1

    def test_run_tests_pass_exits_0(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        result = subprocess.run(
            ["python", str(Path.cwd() / "scripts" / "self_improve.py"),
             "run-tests", str(dest)],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0
        assert "TESTS_PASSED=" in result.stdout

    def test_cleanup_exits_0(self, tmp_path):
        exp = tmp_path / "experiment"
        exp.mkdir()
        result = subprocess.run(
            ["python", str(Path.cwd() / "scripts" / "self_improve.py"),
             "cleanup", str(exp)],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0
        assert not exp.exists()

    def test_commit_cli_roundtrip(self, tmp_path, bare_remote, source_repo):
        from scripts.self_improve import clone_repo, create_branch
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        create_branch(dest, 3)
        (dest / "new.txt").write_text("change")
        result = subprocess.run(
            ["python", str(Path.cwd() / "scripts" / "self_improve.py"),
             "commit", str(dest), "3", "test subject",
             "Decision one|Decision two", "Dr. A|Dr. B", "5",
             "docs/self-improve/minutes.md"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0
        assert "Committed." in result.stdout
