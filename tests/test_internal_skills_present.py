"""Plan 2 Task 4 — internal Memex-mode SKILL.md procedures land on disk.

The three procedure files are agent-facing only (read via the Read tool by
other Atelier skills when Memex mode is active). They are not registered
as Claude Code plugin skills, so the only invariant we can test from
Python is that the files exist and carry the substring markers the
calling skills grep for.
"""

from pathlib import Path

INTERNAL = Path(__file__).resolve().parent.parent / "internal"


def test_internal_memex_dispatch_write_skill_exists():
    p = INTERNAL / "memex" / "dispatch-write" / "SKILL.md"
    assert p.is_file(), f"missing file: {p}"
    text = p.read_text(encoding="utf-8")
    for marker in (
        "memex:index:write",
        "librarian_output",
        'mode="callerbuilt"',
    ):
        assert marker in text, f"dispatch-write missing marker: {marker!r}"


def test_internal_memex_dispatch_core_skill_exists():
    p = INTERNAL / "memex" / "dispatch-core" / "SKILL.md"
    assert p.is_file(), f"missing file: {p}"
    text = p.read_text(encoding="utf-8")
    for marker in (
        "memex:core:insert",
        "memex:core:update",
        "memex:core:query",
        "memex:core:execute",
        "memex:core:register-role",
        "memex:core:register-agent",
    ):
        assert marker in text, f"dispatch-core missing marker: {marker!r}"


def test_internal_bootstrap_memex_skill_exists():
    p = INTERNAL / "bootstrap-memex" / "SKILL.md"
    assert p.is_file(), f"missing file: {p}"
    text = p.read_text(encoding="utf-8")
    for marker in (
        "memex:core:create-store",
        "require_bootstrap",
        "find_or_create_role",
        "find_or_create_agent",
    ):
        assert marker in text, f"bootstrap-memex missing marker: {marker!r}"
