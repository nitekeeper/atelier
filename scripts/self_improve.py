"""Atelier self-improvement cycle — git infrastructure CLI."""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# ── Internal git helper ────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


# ── Public functions ───────────────────────────────────────────────────────

def get_remote_url(repo_dir: Path) -> str:
    """Return the origin remote URL of the production repo."""
    result = _git(["remote", "get-url", "origin"], repo_dir)
    return result.stdout.strip()


def clone_repo(remote_url: str, dest: Path) -> None:
    """Clone remote_url into dest and configure a known git identity."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-b", "main", remote_url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(["config", "user.email", "self-improve@atelier.local"], dest)
    _git(["config", "user.name", "Atelier Self-Improve"], dest)


def create_branch(clone_dir: Path, cycle_n: int) -> str:
    """Create and checkout self-improve/cycle-N-YYYY-MM-DD. Returns branch name."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    branch = f"self-improve/cycle-{cycle_n}-{date_str}"
    _git(["checkout", "-b", branch], clone_dir)
    return branch


def run_tests_in_clone(clone_dir: Path) -> tuple[bool, int]:
    """Run pytest in clone_dir. Returns (all_passed, test_count)."""
    result = subprocess.run(
        ["python", "-m", "pytest", "-v", "--tb=short"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    count = 0
    for line in result.stdout.splitlines():
        m = re.search(r"(\d+) passed", line)
        if m:
            count = int(m.group(1))
    return result.returncode == 0, count


def write_minutes(minutes_path: Path, content: str) -> None:
    """Ensure parent dirs exist and write the meeting minutes Markdown file."""
    minutes_path.parent.mkdir(parents=True, exist_ok=True)
    minutes_path.write_text(content, encoding="utf-8")


def commit_cycle(
    clone_dir: Path,
    cycle_n: int,
    decisions: list[str],
    participants: list[str],
    n_tests: int,
    subject: str,
    minutes_rel_path: str,
) -> None:
    """Stage all changes and produce the standard self-improve commit."""
    _git(["add", "-A"], clone_dir)
    summary = decisions[0] if decisions else "improvements applied"
    decisions_text = "\n".join(f"  {i + 1}. {d}" for i, d in enumerate(decisions))
    msg = (
        f"self-improve(cycle-{cycle_n}): {summary}\n\n"
        f"Meeting: {minutes_rel_path}\n"
        f"Participants: {', '.join(participants)}\n"
        f"Decisions:\n{decisions_text}\n"
        f"Tests: {n_tests} passed\n"
        f"Subject: {subject}"
    )
    _git(["commit", "-m", msg], clone_dir)


def push_branch(clone_dir: Path, branch: str) -> None:
    """Push branch to origin from the clone."""
    _git(["push", "origin", branch], clone_dir)


def auto_merge_to_main(repo_dir: Path, branch: str) -> None:
    """Merge branch into main in the production repo and push. Leaves repo on main.

    Stashes any uncommitted changes in the main workspace before merging and
    restores them afterward so a dirty working tree never blocks the merge.
    """
    _git(["fetch", "origin"], repo_dir)
    _git(["checkout", "main"], repo_dir)
    _git(["pull", "origin", "main"], repo_dir)

    stash_result = _git(["stash", "push", "--include-untracked", "-m", "auto-stash before self-improve merge"], repo_dir, check=False)
    stashed = "No local changes" not in stash_result.stdout and stash_result.returncode == 0

    try:
        _git(["merge", "--no-ff", f"origin/{branch}", "-m", f"Merge {branch} into main"], repo_dir)
        _git(["push", "origin", "main"], repo_dir)
    finally:
        if stashed:
            _git(["stash", "pop"], repo_dir, check=False)


def cleanup_experiment(experiment_dir: Path) -> None:
    """Delete the experiment directory. Safe if it does not exist."""
    if experiment_dir.exists():
        shutil.rmtree(experiment_dir)


def pull_main(repo_dir: Path) -> None:
    """Pull main in the production repo."""
    _git(["pull", "origin", "main"], repo_dir)


if __name__ == "__main__":
    # Usage patterns:
    #   python scripts/self_improve.py clone <cycle_n>
    #   python scripts/self_improve.py check-destructive <clone_dir>
    #   python scripts/self_improve.py run-tests <clone_dir>
    #   python scripts/self_improve.py commit <clone_dir> <cycle_n> <subject> <decisions> <participants> <n_tests> <minutes_path>
    #   python scripts/self_improve.py push-merge <clone_dir> <branch> [skip]
    #   python scripts/self_improve.py cleanup [<experiment_dir>]
    #   python scripts/self_improve.py pull

    import json
    import sys

    from scripts.worktree import detect_worktree, parse_main_worktree

    _cwd = Path.cwd()
    _is_wt, _ = detect_worktree(_cwd)
    repo_dir = Path(parse_main_worktree(_cwd)[0]) if _is_wt else _cwd
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "clone":
        cycle_n = int(sys.argv[2])
        remote_url = get_remote_url(repo_dir)
        experiment_dir = repo_dir.parent / "experiment"
        clone = experiment_dir / repo_dir.name
        clone_repo(remote_url, clone)
        branch = create_branch(clone, cycle_n)
        print(f"CLONE_DIR={clone}")
        print(f"BRANCH={branch}")

    elif cmd == "check-destructive":
        from scripts.destructive_check import get_diff, detect_destructive
        clone = Path(sys.argv[2])
        diff = get_diff(clone)
        issues = detect_destructive(diff, clone)
        print(json.dumps(issues, indent=2))
        if issues:
            sys.exit(1)

    elif cmd == "run-tests":
        clone = Path(sys.argv[2])
        passed, count = run_tests_in_clone(clone)
        print(f"TESTS_PASSED={count}")
        if not passed:
            sys.exit(1)

    elif cmd == "commit":
        # commit <clone_dir> <cycle_n> <subject> "<d1>|<d2>" "<p1>|<p2>" <n_tests> <minutes_path>
        clone = Path(sys.argv[2])
        cycle_n = int(sys.argv[3])
        subject = sys.argv[4]
        decisions = [d for d in sys.argv[5].split("|") if d.strip()]
        participants = [p for p in sys.argv[6].split("|") if p.strip()]
        n_tests = int(sys.argv[7])
        minutes_rel = sys.argv[8]
        commit_cycle(clone, cycle_n, decisions, participants, n_tests, subject, minutes_rel)
        print("Committed.")

    elif cmd == "push-merge":
        clone = Path(sys.argv[2])
        branch = sys.argv[3]
        skip_merge = len(sys.argv) > 4 and sys.argv[4] == "skip"
        push_branch(clone, branch)
        if not skip_merge:
            auto_merge_to_main(repo_dir, branch)
            pull_main(repo_dir)
            print(f"Merged {branch} into main and pulled.")
        else:
            print(f"Branch {branch} pushed. Awaiting human approval to merge.")

    elif cmd == "cleanup":
        exp = Path(sys.argv[2]) if len(sys.argv) > 2 else repo_dir.parent / "experiment"
        cleanup_experiment(exp)
        print("experiment/ removed.")

    elif cmd == "pull":
        pull_main(repo_dir)
        print("Pulled main.")

    else:
        print(
            "Commands: clone, check-destructive, run-tests, commit, "
            "push-merge, cleanup, pull",
            file=sys.stderr,
        )
        sys.exit(1)
