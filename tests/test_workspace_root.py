"""Tests for `scripts.git_utils.find_git_root` + `scripts.workspace.workspace_root`.

Plan 1 Task 7 — workspace resolution helper. Spec §10.2 calls `find_git_root` from
`resolve_scope()`; Plan 3 `create_document` calls `workspace_root()` to locate the
on-disk markdown file before passing the body to `backend.write_document`.
"""
import subprocess
from pathlib import Path

import pytest


def _make_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


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
    # tmp_path is not under any git repo (pytest tmp_path is a fresh tree)
    outside = tmp_path / "no-repo"
    outside.mkdir()
    from scripts.git_utils import find_git_root
    assert find_git_root(outside) is None


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
    with pytest.raises(FileNotFoundError, match="not inside a git repository"):
        workspace_root()
