"""Agent-team-mode dev-finish: per-run feature branch merge + PR + retrospective.

This is the thin, testable git/gh layer behind the ``internal/dev-finish``
team-mode finish sub-procedure (atelier#66 / S1; AC1/AC2/AC7). Keeping the
recipe in the SKILL but the mechanics here means the merge/PR/retro logic is
unit-testable against a real throw-away git repo without standing up a live
team-mode run.

Four surfaces:

* :func:`resolve_or_create_feature_branch` — read the canonical
  ``atelier/<slug>`` branch back via ``git branch --list`` (F4-analog: NEVER
  re-derive the slug ad hoc), creating it off ``base`` only when absent.
* :func:`merge_worktrees` — merge N task branches into the per-run feature
  branch (NOT into ``base``) in dependency order, ``--no-ff`` with a clean
  ``git merge --abort`` on conflict. Clean worktrees are auto-removed; DIRTY
  ones are PRESERVED and listed for the PR body. Dirtiness reuses
  :func:`scripts.worktree.classify_status`'s ``?? .claude/``-aware split so a
  worktree dirty only with harness storage counts CLEAN.
* :func:`open_pr` — a single ``gh pr create --base --head`` against the feature
  branch, fail-loud on non-zero / empty URL. The subprocess runner is
  injectable so tests never shell out.
* :func:`write_retrospective` — ONE ``backend.write_document(domain='project_doc',
  subdomain='finish-result', ...)`` via the A2 facade. Mode-symmetric: there is
  NO mode branch here (the facade routes Local vs Memex), so the retro doc is
  durable in both modes.

Design risks this module is built to avoid (see /tmp/atelier-66-design.md):

* It does NOT call :func:`scripts.worktree.merge_back` — that merges into BASE
  and DELETES the branch, which would skip the PR and push straight to main.
  ``merge_worktrees`` targets the ``atelier/<slug>`` FEATURE branch and KEEPS
  it for the PR.
* The git/PR ops are inherently Local-path operations (worktrees only exist for
  a live Local team-mode run) but they are NOT team-STATE mutators, so they do
  NOT inherit ``team_teardown``'s Local-only mode-gate. Only the durable retro
  write crosses modes, and it does so symmetrically via the facade.

All product DB writes route through :mod:`scripts.backend` (A2). No raw SQL
lives in this module.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from scripts import backend
from scripts.git_utils import git as _git
from scripts.worktree import classify_status

__all__ = (
    "MergeResult",
    "merge_worktrees",
    "open_pr",
    "resolve_or_create_feature_branch",
    "task_branch_name",
    "write_retrospective",
)

# ── Sibling-namespace branch contract (atelier#66 N0/N2) ────────────────────
#
# The per-run feature branch is `atelier/<slug>`; task branches live in a
# SIBLING namespace `atelier/<slug>-task-<task_id>` (NOT nested under the
# feature branch — git's loose-ref storage forbids both a file
# `refs/heads/atelier/<slug>` and a directory `refs/heads/atelier/<slug>/...`).
#
# This anchored regex is the merge-boundary guard. It serves TWO purposes at
# once: (1) it enforces the sibling scheme, and (2) it is a defense-in-depth
# option-injection block — a branch/ref literally named like a git option
# (e.g. `-X`, `--output=..`) cannot match the leading `atelier/` literal, so it
# is rejected BEFORE it ever reaches a `_git` positional. `shell=False` already
# holds (no shell metachar interpretation); this closes the orthogonal "a
# positional that git itself parses as an OPTION" gap, complementing the `--`
# end-of-options separators added to the `_git` positionals below.
#
# Grammar: `atelier/` + a slug that does NOT start with `-` (leading-dash names
# are the classic option-injection vector) followed by an OPTIONAL
# `-task-<task_id>` suffix whose id also does not start with `-`.
_BRANCH_RE = re.compile(r"^atelier/[a-z0-9][a-z0-9-]*(-task-[A-Za-z0-9][A-Za-z0-9-]*)?$")


def task_branch_name(slug: str, task_id: str) -> str:
    """Return the canonical SIBLING-namespace task-branch name (atelier#66 N2).

    ``atelier/<slug>-task-<task_id>`` — a SIBLING of the feature branch
    ``atelier/<slug>``, NOT the colliding nested ``atelier/<slug>/<task_id>``
    (git's loose-ref storage cannot hold both a ref FILE at
    ``refs/heads/atelier/<slug>`` and a ref DIRECTORY ``refs/heads/atelier/<slug>/``).

    This is the single source of truth for the task-branch scheme: the
    merge-boundary validation in :func:`merge_worktrees` enforces it via
    ``_BRANCH_RE``, and ``internal/dev-finish/SKILL.md`` references it so the
    FUTURE worktree-creation dispatch layer is forced through this helper rather
    than re-deriving the slug ad hoc."""
    return f"atelier/{slug}-task-{task_id}"


def _validate_branch(branch: str, *, role: str) -> str:
    """Validate ``branch`` against the sibling-namespace contract (atelier#66 N0).

    Raises :class:`ValueError` on a non-conforming or leading-dash name. A
    leading-dash name is the classic git option-injection vector (a positional
    parsed as an OPTION); rejecting it here is defense-in-depth on top of the
    ``--`` end-of-options separators and ``shell=False``. ``role`` names the
    branch's purpose for a clear error message. Returns the branch unchanged on
    success so callers can inline the guard."""
    if not _BRANCH_RE.match(branch):
        raise ValueError(
            f"refusing {role} branch {branch!r}: it does not match the "
            f"sibling-namespace contract {_BRANCH_RE.pattern!r} (a leading-dash "
            "or otherwise non-conforming name is rejected — it could be parsed "
            "as a git option and breaks the atelier/<slug>[-task-<id>] scheme)"
        )
    return branch


# A subprocess runner: (cmd, cwd) -> CompletedProcess. Injectable so tests can
# stub the git push + gh pr create without shelling out / hitting the network.
Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@dataclass
class MergeResult:
    """Outcome of :func:`merge_worktrees`.

    * ``merged`` — task branches successfully merged into the feature branch,
      in the dependency order they were attempted.
    * ``dirty_preserved`` — ``(worktree_path, porcelain_status)`` pairs for
      worktrees PRESERVED because they carried uncommitted PROJECT changes
      (``.claude/``-only dirt does NOT count). Rendered into the PR body so the
      operator knows what was left on disk.
    * ``conflicts`` — task branches whose merge conflicted and was aborted
      (``git merge --abort``); the feature branch is left at the last clean
      merge and the conflicting worktree is preserved.
    """

    merged: list[str] = field(default_factory=list)
    dirty_preserved: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def resolve_or_create_feature_branch(repo_root: Path, slug: str, base: str) -> str:
    """Return the canonical per-run feature branch ``atelier/<slug>``.

    Reads the branch back via ``git branch --list`` (F4-analog: the canonical
    string is read from git state, never re-derived from the slug downstream).
    Creates it off ``base`` with ``git branch`` only when absent. ``repo_root``
    is the main worktree (the place ``atelier/<slug>`` and the task worktree
    branches share as their object store)."""
    branch = _validate_branch(f"atelier/{slug}", role="feature")
    listed = _git(["branch", "--list", branch], repo_root).stdout.strip()
    if listed:
        return branch
    # Create off base WITHOUT switching the working tree (the main worktree may
    # be on `base` and the task worktrees own their own branches). `--`
    # end-of-options separator guards the branch-name + start-point positionals
    # against option-injection (defense-in-depth; shell=False holds).
    _git(["branch", "--", branch, base], repo_root)
    return branch


def _worktree_status(worktree: Path) -> tuple[bool, str]:
    """Return ``(is_dirty_with_project_changes, porcelain_status)``.

    Reuses :func:`scripts.worktree.classify_status`'s ``?? .claude/``-aware
    split: a worktree is "project-dirty" only when it has tracked-file changes
    (``dirty``) OR non-``.claude/`` untracked files (``untracked_other``).
    Untracked ``.claude/`` harness storage is ignored — it is Claude Code's
    worktree-local storage, not project work — so such a worktree counts CLEAN
    and is auto-removed."""
    porcelain = _git(["status", "--porcelain"], worktree).stdout
    dirty, _untracked_claude, untracked_other = classify_status(porcelain)
    is_project_dirty = bool(dirty or untracked_other)
    return is_project_dirty, porcelain.strip()


def _worktree_path_for_branch(repo_root: Path, branch: str) -> Path | None:
    """Resolve the linked-worktree path checked out on ``branch``, or None."""
    out = _git(["worktree", "list", "--porcelain"], repo_root).stdout
    normalised = out.replace("\r\n", "\n").replace("\r", "\n")
    path: str | None = None
    for block in normalised.strip().split("\n\n"):
        wt_path: str | None = None
        wt_branch: str | None = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                wt_path = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                wt_branch = line.split(" ", 1)[1].replace("refs/heads/", "")
        if wt_branch == branch and wt_path:
            path = wt_path
            break
    return Path(path) if path else None


def merge_worktrees(
    repo_root: Path,
    feature_branch: str,
    base: str,
    task_branches_in_dep_order: list[str],
) -> MergeResult:
    """Merge ``task_branches_in_dep_order`` into ``feature_branch`` (NOT base).

    For each task branch, in dependency order:

    1. ``git merge --no-ff <task_branch>`` onto ``feature_branch`` in the main
       worktree. On conflict: ``git merge --abort`` (clean rollback), record the
       branch in ``conflicts``, PRESERVE its worktree, and continue.
    2. On clean merge: record in ``merged``, then inspect the task's worktree —
       if it carries uncommitted PROJECT changes (``.claude/``-aware split),
       PRESERVE it and collect ``(path, status)``; otherwise ``git worktree
       remove --force`` it.

    The main worktree is switched onto ``feature_branch`` for the duration of
    the merges (and restored to its original branch afterwards), so the merges
    land on ``feature_branch``. ``base`` is accepted for parity / future use but
    is NEVER the merge TARGET — the critical risk this module exists to avoid is
    merging into base (which would skip the PR and push straight to main).
    """
    result = MergeResult()

    # ── Merge-boundary guard (atelier#66 N0) ─────────────────────────────────
    # Anchor the sibling-namespace contract HERE, at the boundary where these
    # strings become `_git` POSITIONALS. This simultaneously (a) enforces the
    # atelier/<slug>[-task-<id>] scheme and (b) blocks git option-injection (a
    # leading-dash branch that git would parse as an OPTION). Validate BEFORE any
    # checkout / merge so a bad name fails loud and the working tree is untouched.
    _validate_branch(feature_branch, role="feature")
    for task_branch in task_branches_in_dep_order:
        _validate_branch(task_branch, role="task")

    # Capture the original branch FIRST (atelier#66 N4) — BEFORE the
    # feature-branch checkout — so the try/finally below ALWAYS restores it even
    # when the feature-branch checkout itself fails.
    current = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root).stdout.strip()

    try:
        # Switch the main worktree onto the feature branch so merges land there.
        # The task worktrees own their own branches, so this never collides.
        # This is INSIDE the try (N4): a checkout failure here must still hit the
        # finally and restore `current`. Branch-switch checkout takes the branch
        # as an operand (NOT a pathspec), so a `--` separator would change its
        # semantics to pathspec — instead the branch is guarded by _BRANCH_RE
        # above, which already rejects any leading-dash / option-like name.
        if current != feature_branch:
            _git(["checkout", feature_branch], repo_root)

        for task_branch in task_branches_in_dep_order:
            merge = _git(
                [
                    "merge",
                    "--no-ff",
                    "-m",
                    f"Merge {task_branch} into {feature_branch}",
                    # `--` end-of-options separator: everything after it is a
                    # positional, so a branch literally named like an option can
                    # never be parsed as one (defense-in-depth; shell=False holds).
                    "--",
                    task_branch,
                ],
                repo_root,
                check=False,
            )
            if merge.returncode != 0:
                # Conflict (or other merge failure): roll back cleanly, preserve
                # the worktree, and carry on with the remaining tasks.
                _git(["merge", "--abort"], repo_root, check=False)
                result.conflicts.append(task_branch)
                continue

            result.merged.append(task_branch)

            wt = _worktree_path_for_branch(repo_root, task_branch)
            if wt is None or not wt.exists():
                # No live worktree (already removed / never linked) — nothing
                # to clean up.
                continue
            is_dirty, status = _worktree_status(wt)
            if is_dirty:
                result.dirty_preserved.append((str(wt), status))
            else:
                # `--` separates the (option) flags from the PATH positional so a
                # worktree path that looks like an option cannot be parsed as one.
                _git(
                    ["worktree", "remove", "--force", "--", str(wt)],
                    repo_root,
                    check=False,
                )
    finally:
        # Restore the main worktree to its original branch so the caller's
        # session state is not silently moved. Runs on ANY failure above —
        # including a failed feature-branch checkout (N4).
        if current and current != feature_branch:
            _git(["checkout", current], repo_root, check=False)

    return result


def open_pr(
    repo_root: Path,
    feature_branch: str,
    base: str,
    title: str,
    body: str,
    *,
    remote: str = "origin",
    runner: Runner | None = None,
) -> str:
    """Push ``feature_branch`` then open a single PR via ``gh pr create``.

    Returns the PR URL (gh prints it as the last non-empty stdout line). Raises
    :class:`RuntimeError` on a non-zero push/gh exit or an empty URL. The
    ``runner`` is injectable so tests can stub both subprocess calls.
    """
    run = runner or _default_runner

    # Guard the branch at the push/PR boundary too (atelier#66 N0): a malformed
    # feature_branch must not reach `git push` / `gh pr create` as a positional.
    _validate_branch(feature_branch, role="feature")

    # `--` end-of-options separator: <remote> + <refspec> are positionals, so a
    # branch named like an option cannot be parsed as one (shell=False holds).
    push = run(["git", "push", "-u", remote, "--", feature_branch], repo_root)
    if push.returncode != 0:
        raise RuntimeError(
            f"git push of {feature_branch!r} failed (exit {push.returncode}): "
            f"{(push.stderr or '').strip()}"
        )

    gh = run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            feature_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        repo_root,
    )
    if gh.returncode != 0:
        raise RuntimeError(
            f"gh pr create failed (exit {gh.returncode}): {(gh.stderr or '').strip()}"
        )
    lines = [ln.strip() for ln in (gh.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("gh pr create returned exit 0 but no URL on stdout")
    return lines[-1]


def write_retrospective(
    *,
    workspace_id: int | None,
    project_id: int | None,
    title: str,
    body: str,
    pr_url: str | None,
    caller_agent_id: str = "dev-finish",
    extra_metadata: dict[str, object] | None = None,
) -> dict:
    """Persist the team-mode finish retrospective as ONE durable doc (AC2).

    A single ``backend.write_document(domain='project_doc',
    subdomain='finish-result', ...)`` via the A2 facade. The facade routes
    Local vs Memex with no mode branch here, so the retro is durable in both
    modes (Memex folds ``subdomain`` + ``project_id`` into the Index metadata
    blob; Local writes the wide signature). ``metadata`` carries
    ``phase='finish'`` and the ``pr_url`` so the artifact is self-describing.
    Returns the backend echo dict (``row_id`` / ``index_id``).
    """
    metadata: dict[str, object] = {"phase": "finish"}
    if pr_url is not None:
        metadata["pr_url"] = pr_url
    if extra_metadata:
        metadata.update(extra_metadata)
    return backend.write_document(
        workspace_id=workspace_id,
        project_id=project_id,
        domain="project_doc",
        subdomain="finish-result",
        title=title,
        body=body,
        metadata=metadata,
        caller_agent_id=caller_agent_id,
    )
