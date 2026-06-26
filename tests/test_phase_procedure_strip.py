"""Regression tests for phase-procedure boilerplate stripping at injection.

`compose_briefing` trims worker-irrelevant boilerplate from `phase_procedure_text`
before injecting it: YAML frontmatter, the Prerequisites "Mode:"/"Required tables:"
implementation-detail lines, and the pre-dispatch "1. Check the phase gate:" step
(the soft-wall/bypass check the orchestrator runs before spawning — a worker never
runs check-gate). The actual implementation steps and Hard rules are preserved.
"""

from scripts.dispatch import _strip_worker_irrelevant_phase, compose_briefing

_KW = {
    "role_id": "backend-engineer-1",
    "task_id": "t-1",
    "persona_profile_text": "(persona)",
    "task_brief": "(task)",
    "team_id": "team-1",
    "team_lead_name": "PM",
    "wave_id": "w-1",
    "wave_phase": "tdd:green",
    "deadline_iso": "2099-12-31T23:59:59+00:00",
    "transport": "cli",
}

_SAMPLE_PHASE = """\
---
description: Use when implementing a plan task.
---

# dev:tdd

> **Prerequisites**
> - Mode: Memex or Local (mode-symmetric — dispatch via backend.py)
> - Required: `plan:approved` phase reached; plan document readable
> - Required tables: `projects`, `skill_gates` — seeded by Atelier bootstrap

## The Iron Law

No production code without a failing test first.

## Procedure

1. Check the phase gate:
   ```
   python3 atelier/scripts/workflow.py <db_path> check-gate <project_id> dev:tdd
   ```
   **If `allowed` is `false`** (soft wall): ask the user to proceed or advance.
   - On no: stop. Tell the user to advance first.

2. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance ...`

3. Read the plan document and work one task at a time.

## Hard rules

- The Iron Law is non-negotiable.
"""


def test_strip_removes_boilerplate_keeps_procedure():
    out = _strip_worker_irrelevant_phase(_SAMPLE_PHASE)
    # Boilerplate gone.
    assert not out.startswith("---")
    assert "description: Use when" not in out
    assert "Mode: Memex or Local" not in out
    assert "Required tables:" not in out
    assert "Check the phase gate" not in out
    assert "check-gate" not in out
    assert "log-bypass" not in out  # bypass block gone
    assert "soft wall" not in out
    # Load-bearing content kept.
    assert "The Iron Law" in out
    assert "No production code without a failing test first" in out
    assert "2. Advance phase" in out
    assert "3. Read the plan document" in out
    assert "## Hard rules" in out
    # The load-bearing prereq line survives.
    assert "Required: `plan:approved`" in out


def test_strip_is_idempotent():
    once = _strip_worker_irrelevant_phase(_SAMPLE_PHASE)
    assert _strip_worker_irrelevant_phase(once) == once


def test_no_gate_step_degrades_safely():
    # A procedure without a gate step is returned with only frontmatter/prereq
    # strips applied — no content dropped.
    text = "# dev:doc\n\n## Procedure\n\n1. Write the doc.\n\n2. Done.\n"
    out = _strip_worker_irrelevant_phase(text)
    assert "1. Write the doc." in out and "2. Done." in out


def test_real_phase_files_strip_gate_keep_steps():
    """Lock in the strip against the REAL on-disk dev-* procedures so a future
    rewording of the gate step / prereq lines can't silently no-op the strip."""
    import pathlib

    import scripts.dispatch as d

    base = pathlib.Path(d.__file__).resolve().parent.parent / "internal"
    for phase in ("dev-tdd", "dev-review", "dev-security", "dev-qa"):
        raw = (base / phase / "SKILL.md").read_text(encoding="utf-8")
        assert "1. Check the phase gate:" in raw  # precondition: the real file has it
        out = _strip_worker_irrelevant_phase(raw)
        assert "Check the phase gate" not in out, phase
        assert "check-gate" not in out, phase
        assert "Mode: Memex or Local" not in out, phase
        # The implementation steps the worker runs survive.
        assert "2. Advance phase" in out, phase
        assert "## Hard rules" in out, phase


def test_compose_briefing_injects_stripped_phase():
    # End-to-end: the real dev-tdd procedure passed through compose_briefing
    # comes out without the gate-check step but with the implementation steps.
    briefing = compose_briefing(phase_procedure_text=_SAMPLE_PHASE, team_chat=None, **_KW)
    assert "check-gate" not in briefing
    assert "The Iron Law" in briefing
    assert "Advance phase" in briefing
