"""Plan 2 Task 4 — internal Memex-mode SKILL.md procedures land on disk.

The three procedure files are agent-facing only (read via the Read tool by
other Atelier skills when Memex mode is active). They are not registered
as Claude Code plugin skills, so the only invariant we can test from
Python is that the files exist and carry the substring markers the
calling skills grep for.
"""

from pathlib import Path

INTERNAL = Path(__file__).resolve().parent.parent / "internal"


def test_internal_memex_dispatch_write_skill_present_with_markers():
    p = INTERNAL / "memex" / "dispatch-write" / "SKILL.md"
    assert p.is_file(), f"missing file: {p}"
    text = p.read_text(encoding="utf-8")
    for marker in (
        "memex:index:write",
        "librarian_output",
        'mode="callerbuilt"',
    ):
        assert marker in text, f"dispatch-write missing marker: {marker!r}"


def test_internal_memex_dispatch_core_skill_present_with_markers():
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


def test_internal_bootstrap_memex_skill_present_with_markers():
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


def test_internal_bridge_poll_skill_present_with_markers():
    """atelier#81 — the orchestrator-side per-turn servicing procedure for the
    production dispatch queue. Agent-facing only (read via the Read tool); the
    only Python-testable invariant is presence + the substring markers the
    orchestrator turn-loop greps for."""
    p = INTERNAL / "bridge-poll" / "SKILL.md"
    assert p.is_file(), f"missing file: {p}"
    text = p.read_text(encoding="utf-8")
    for marker in (
        "bridge_requests",
        # closed-enum switch: the four DispatchTools method names == kind enum.
        "create_team",
        "spawn_teammate",
        "send_message",
        "spawn_subagent",
        # the three encoded contracts.
        "fail-safe-pending",
        "READ-FIRST",
        "untrusted",
        # references the REAL companion procedure, NOT the nonexistent run/ path.
        "internal/pm-dispatch/SKILL.md",
    ):
        assert marker in text, f"bridge-poll missing marker: {marker!r}"
    # Guard against the planning bug that abandoned the prior cycle: this SKILL
    # MUST NOT reference internal/run/SKILL.md (that path does not exist in
    # atelier — public skills live under skills/run/, not internal/run/).
    assert "internal/run/SKILL.md" not in text, (
        "bridge-poll references the nonexistent internal/run/SKILL.md path"
    )
