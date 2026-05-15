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
    """Merge branch into main in the production repo and push. Leaves repo on main."""
    _git(["fetch", "origin"], repo_dir)
    _git(["checkout", "main"], repo_dir)
    _git(["pull", "origin", "main"], repo_dir)
    _git(["merge", "--no-ff", f"origin/{branch}", "-m", f"Merge {branch} into main"], repo_dir)
    _git(["push", "origin", "main"], repo_dir)


def cleanup_experiment(experiment_dir: Path) -> None:
    """Delete the experiment directory. Safe if it does not exist."""
    if experiment_dir.exists():
        shutil.rmtree(experiment_dir)


def pull_main(repo_dir: Path) -> None:
    """Pull main in the production repo."""
    _git(["pull", "origin", "main"], repo_dir)
