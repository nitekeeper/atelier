"""Tests for the SessionStart hook."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "session_start.py"


def test_hook_outputs_skill_body():
    """Hook stdout contains the body of using-atelier/SKILL.md."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 0, f"hook failed; stderr: {result.stderr}"
    # Body must contain the canonical sections
    for section in ["## Trigger contract", "## Red Flags",
                    "## Phase guidance", "## Dev arc", "## Bypass procedure"]:
        assert section in result.stdout, f"missing section in hook output: {section}"


def test_hook_does_not_emit_frontmatter():
    """The frontmatter delimiters and YAML keys should not appear in injected context."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    # The hook strips the frontmatter block before printing
    assert "name: using-atelier" not in result.stdout
    assert not result.stdout.lstrip().startswith("---")


def test_hook_exits_zero_when_skill_missing(tmp_path):
    """If using-atelier/SKILL.md is missing, hook exits 0 (does not block session)."""
    # Create a fake atelier root in tmp_path with NO skills/using-atelier/SKILL.md
    (tmp_path / "skills").mkdir()
    (tmp_path / "hooks").mkdir()
    target_hook = tmp_path / "hooks" / "session_start.py"
    target_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(target_hook)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    # Must NOT block session even if the canonical file is missing
    assert result.returncode == 0, f"hook returned non-zero on missing skill; stderr: {result.stderr}"
