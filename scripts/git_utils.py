"""Shared git subprocess helper used by self_improve and worktree modules."""
from __future__ import annotations

import subprocess
from pathlib import Path


def git(args: list[str], cwd: Path, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        **kwargs,
    )


def find_git_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default CWD) until a directory containing a `.git`
    entry (file or directory) is found. Returns the resolved path of that
    directory, or None if the walk hits filesystem root.

    Used by `scripts.workspace.workspace_root()` and `scripts.scope.resolve_scope()`
    (spec §10.2). A `.git` *file* (not directory) indicates a submodule or linked
    worktree — both still resolve to a valid workspace per the spec's
    workspace-identity rules.

    Note: bare repositories (no `.git` entry, repo files live at the root) are
    not detected by this O(depth) walk. Atelier does not run inside bare repos
    in practice, so the limitation is accepted.
    """
    cur = Path(start).resolve() if start else Path.cwd().resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return None


def git_remote_url(root: Path) -> str | None:
    """Return `origin`'s URL for the repo at `root`, or None if no remote is
    configured. Used by spec §10.2's workspace identity rule
    (`identity = git_remote_url(root) or str(root)`).
    """
    res = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    url = res.stdout.strip()
    return url or None
