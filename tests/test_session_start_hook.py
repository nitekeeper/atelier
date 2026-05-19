"""Tests for the SessionStart hook."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "session_start.py"


def test_hook_outputs_skill_body():
    """Hook stdout contains the body of run/SKILL.md."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, f"hook failed; stderr: {result.stderr}"
    # Body must contain the canonical sections
    for section in [
        "## Trigger contract",
        "## Red Flags",
        "## Phase guidance",
        "## Dev arc",
        "## Bypass procedure",
    ]:
        assert section in result.stdout, f"missing section in hook output: {section}"


def test_hook_does_not_emit_frontmatter():
    """The frontmatter delimiters and YAML keys should not appear in injected context."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    # The hook strips the frontmatter block before printing
    assert "name: execute" not in result.stdout
    assert not result.stdout.lstrip().startswith("---")


def test_hook_exits_zero_when_skill_missing(tmp_path):
    """If run/SKILL.md is missing, hook exits 0 (does not block session)."""
    # Create a fake atelier root in tmp_path with NO skills/run/SKILL.md
    (tmp_path / "skills").mkdir()
    (tmp_path / "hooks").mkdir()
    target_hook = tmp_path / "hooks" / "session_start.py"
    target_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(target_hook)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONUTF8": "1"},
    )
    # Must NOT block session even if the canonical file is missing
    assert result.returncode == 0, (
        f"hook returned non-zero on missing skill; stderr: {result.stderr}"
    )


def test_hook_strips_frontmatter_with_crlf_line_endings(tmp_path):
    """Hook strips frontmatter even when SKILL.md has CRLF line endings (Windows checkout)."""
    # Create a fake atelier root with a CRLF-encoded SKILL.md
    skill_dir = tmp_path / "skills" / "run"
    skill_dir.mkdir(parents=True)
    crlf_content = "---\r\nname: initiate\r\ndescription: Test\r\n---\r\n# Body\r\n## Trigger contract\r\nContent here\r\n"
    (skill_dir / "SKILL.md").write_bytes(crlf_content.encode("utf-8"))

    (tmp_path / "hooks").mkdir()
    target_hook = tmp_path / "hooks" / "session_start.py"
    target_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(target_hook)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0
    # Frontmatter must be stripped even with CRLF
    assert "name: execute" not in result.stdout, (
        f"frontmatter leaked into output: {result.stdout[:200]!r}"
    )
    assert "## Trigger contract" in result.stdout, "body content missing"
