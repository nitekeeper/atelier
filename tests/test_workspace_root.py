"""Tests for `scripts.git_utils.find_git_root` + `scripts.git_utils.git_remote_url`
+ `scripts.workspace.workspace_root`.

Plan 1 Task 7 — workspace resolution helper. Spec §10.2 calls `find_git_root` from
`resolve_scope()`; Plan 3 `create_document` calls `workspace_root()` to locate the
on-disk markdown file before passing the body to `backend.write_document`.
"""

import re
import subprocess
from pathlib import Path

import pytest


def _make_git_repo(root: Path) -> None:
    """Create a git repo at `root` with user.email/user.name configured.

    Matches the setup style of `tests/test_worktree.py::_git` so that commits
    work without falling back to the host's global git config.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)


# ── find_git_root ─────────────────────────────────────────────────────────


def test_find_git_root_returns_root_from_root(tmp_path):
    _make_git_repo(tmp_path / "repo")
    from scripts.git_utils import find_git_root

    assert find_git_root(tmp_path / "repo") == (tmp_path / "repo").resolve()


def test_find_git_root_returns_root_from_subdir(tmp_path):
    _make_git_repo(tmp_path / "repo")
    sub = tmp_path / "repo" / "src" / "deep"
    sub.mkdir(parents=True)
    from scripts.git_utils import find_git_root

    assert find_git_root(sub) == (tmp_path / "repo").resolve()


def test_find_git_root_returns_none_outside_repo(tmp_path):
    # tmp_path is not under any git repo (pytest tmp_path is a fresh tree).
    outside = tmp_path / "no-repo"
    outside.mkdir()
    from scripts.git_utils import find_git_root

    result = find_git_root(outside)
    assert result is None, (
        f"Expected None; got {result}. tmp_path={tmp_path} may be inside a git repo "
        "(check pytest basetemp configuration)."
    )


def test_find_git_root_in_linked_worktree(tmp_path):
    """find_git_root returns the linked worktree path (not the main repo path).

    A linked worktree has a `.git` *file* (not directory) pointing to the main
    repo's `.git/worktrees/<name>/`. Spec §6.8 + §10.2 require the linked path
    be returned for filesystem co-location of project documents.
    """
    main = tmp_path / "main"
    _make_git_repo(main)
    # Need a commit to add a worktree.
    subprocess.run(
        ["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "feat", str(wt)],
        check=True,
        capture_output=True,
    )
    from scripts.git_utils import find_git_root

    result = find_git_root(wt)
    assert result == wt.resolve()


# ── git_remote_url ────────────────────────────────────────────────────────


def test_git_remote_url_returns_url_when_remote_configured(tmp_path):
    """git_remote_url returns the origin URL when one is set."""
    repo = tmp_path / "repo"
    _make_git_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@example.com:o/r.git"],
        check=True,
        capture_output=True,
    )
    from scripts.git_utils import git_remote_url

    assert git_remote_url(repo) == "git@example.com:o/r.git"


def test_git_remote_url_returns_none_when_no_origin(tmp_path):
    """git_remote_url returns None when origin is not configured."""
    repo = tmp_path / "repo"
    _make_git_repo(repo)
    from scripts.git_utils import git_remote_url

    assert git_remote_url(repo) is None


def test_git_remote_url_returns_none_on_non_repo(tmp_path):
    """git_remote_url returns None when called on a non-repo path."""
    from scripts.git_utils import git_remote_url

    assert git_remote_url(tmp_path) is None


# ── workspace_root ────────────────────────────────────────────────────────


def test_workspace_root_returns_git_root(tmp_path, monkeypatch):
    _make_git_repo(tmp_path / "repo")
    monkeypatch.chdir(tmp_path / "repo")
    from scripts.workspace import workspace_root

    assert workspace_root() == (tmp_path / "repo").resolve()


def test_workspace_root_raises_outside_git(tmp_path, monkeypatch):
    outside = tmp_path / "no-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    from scripts.workspace import workspace_root

    with pytest.raises(FileNotFoundError, match=re.escape("not inside a git repository")):
        workspace_root()
