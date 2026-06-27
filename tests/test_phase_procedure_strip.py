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
    rewording of the gate step / prereq lines can't silently no-op the strip.

    The gate-step + prereq strips apply to EVERY phase; the cycle-2 advance-phase
    strip (F5) is SCOPED to ``tdd:`` advance targets, so the non-tdd phases keep
    their own ``2. Advance phase`` step (proving F5 leaves them untouched)."""
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
        assert "## Hard rules" in out, phase
        if phase == "dev-tdd":
            # F5: the dev-tdd ``tdd:`` advance-phase host-action commands are gone…
            assert "advance <project_id> tdd:" not in out, phase
            assert "Advance phase" not in out, phase
            # …while the load-bearing surface F5 MUST NOT touch survives.
            assert "## The Iron Law" in out
            assert "pytest -q --tb=short" in out
            assert "### Red cycle" in out and "### Green cycle" in out
            assert "### Clean cycle" in out
        else:
            # Non-tdd phases advance to non-``tdd:`` targets → F5 is a no-op, so
            # their implementation step "2. Advance phase" still survives.
            assert "2. Advance phase" in out, phase


def test_compose_briefing_injects_stripped_phase():
    # End-to-end: the real dev-tdd procedure passed through compose_briefing
    # comes out without the gate-check step but with the implementation steps.
    # _SAMPLE_PHASE's advance step targets a placeholder (not a ``tdd:`` phase),
    # so the cycle-2 F5 strip leaves it in place here.
    briefing = compose_briefing(phase_procedure_text=_SAMPLE_PHASE, team_chat=None, **_KW)
    assert "check-gate" not in briefing
    assert "The Iron Law" in briefing
    assert "Advance phase" in briefing


# ── F5 (cycle 2) — strip the dev-tdd ``tdd:`` advance-phase HOST-action steps ──

_TDD_PHASE = """\
# dev:tdd

## The Iron Law

No production code without a failing test first.

## Procedure

2. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:red`

3. Write the failing test.

### Green cycle

6. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:green`

7. Run the full suite:
   ```
   pytest -q --tb=short
   ```

### Repeat or advance

14. If more tasks remain:
    - Advance phase back to red: `python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:red`
    - Return to step 4.
"""


def test_f5_strips_tdd_advance_commands_keeps_load_bearing():
    """The ``tdd:`` advance-phase command lines (unfilled <db_path>/<project_id>
    HOST actions a worker cannot run) are removed; the Iron Law, the full-suite
    ``pytest -q --tb=short`` step, and the ``### *cycle`` headers are untouched."""
    out = _strip_worker_irrelevant_phase(_TDD_PHASE)
    # Both the numbered-step and the "- Advance phase back to red:" sub-bullet go.
    assert "Advance phase" not in out
    assert "advance <project_id> tdd:" not in out
    # Load-bearing content F5 MUST NOT touch.
    assert "## The Iron Law" in out
    assert "No production code without a failing test first" in out
    assert "pytest -q --tb=short" in out
    assert "### Green cycle" in out
    # The non-advance steps survive (renumbering gaps are cosmetic, like the gate
    # strip): step 3 / 7 keep their numbers.
    assert "3. Write the failing test." in out
    assert "7. Run the full suite:" in out


def test_f5_is_scoped_to_tdd_targets_non_tdd_advance_survives():
    """F5 matches only ``tdd:`` advance targets — an advance line to a non-tdd
    phase (review/security/qa/design/plan) is LEFT IN PLACE, so non-tdd phase
    renders are unaffected by F5."""
    non_tdd = (
        "# dev:review\n\n## Procedure\n\n"
        "2. Advance phase: `python3 atelier/scripts/workflow.py <db_path> advance "
        "<project_id> review:open`\n\n3. Do the review.\n"
    )
    out = _strip_worker_irrelevant_phase(non_tdd)
    assert "2. Advance phase" in out
    assert "advance <project_id> review:open" in out


def test_f5_is_idempotent():
    once = _strip_worker_irrelevant_phase(_TDD_PHASE)
    assert _strip_worker_irrelevant_phase(once) == once


def test_f5_does_not_over_match_lines_without_the_marker_prefix():
    """NEGATIVE / over-match guard — the strip requires the
    ``(?:\\d+\\.|-) Advance phase`` MARKER PREFIX, not merely the keywords. Two
    lines that each carry all three keywords (``workflow.py`` + ``advance`` +
    ``tdd:``) but are NOT marker-prefixed standalone "Advance phase" command steps
    — a fenced reference command and a prose sentence — must NOT match the regex
    and must survive ``_strip_worker_irrelevant_phase`` intact."""
    import scripts.dispatch as d

    fenced_cmd = "python3 atelier/scripts/workflow.py <db_path> advance <project_id> tdd:green"
    prose = (
        "Advance phase here is HOST bookkeeping — workflow.py advance "
        "<project_id> tdd:red is run by the host, never by you."
    )
    text = (
        "# dev:tdd\n\n## Procedure\n\n"
        "4. For reference only, the host advances phases by running:\n"
        f"   ```\n   {fenced_cmd}\n   ```\n\n"
        f"{prose}\n"
    )
    # Neither keyword-bearing line is a marker-prefixed "Advance phase" command.
    assert d._PHASE_ADVANCE_STEP_RE.findall(text) == []
    out = _strip_worker_irrelevant_phase(text)
    # Both survive verbatim — the regex did not over-match on keywords alone.
    assert fenced_cmd in out
    assert prose in out
