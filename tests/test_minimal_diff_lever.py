"""Phase-gated + toggleable output-side minimal-diff lever (M8 rec #3).

``_MINIMAL_DIFF_RULE`` (the minimal-diff/native-first ladder + anti-deliberation
reflex + safety carve-out) appends to an implementer briefing ONLY for an
implementation ``wave_phase`` (tdd / tdd:green / tdd:clean) AND only when
``include_minimal_diff`` is True. A designer / reviewer / test-author must never
be told to minimize code. Each case carries a NEUTER assertion so a silent gate
break — always-on, never-on, wrong phase set, or a dropped safety carve-out —
turns the suite RED.
"""

from __future__ import annotations

from pathlib import Path

from scripts.cli_dispatch import _host_briefing_for
from scripts.dispatch import (
    _CLI_TRANSPORT_RULE,
    _MINIMAL_DIFF_RULE,
    _TERSE_OUTPUT_RULE,
    TRANSPORT_CLI,
    compose_briefing,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Phases that must NOT receive the minimal-diff rule. tdd:red (test DESIGN) is the
# load-bearing exclusion — a test author choosing the right failing test must never
# be told to minimize code; it stays excluded as a verbatim PHASE_TIER key, and the
# absent-check below regression-guards it (a future PHASE_TIER edit that dropped
# tdd:red would let normalize_phase collapse it to "tdd" → gate TRUE → this test RED).
_NON_IMPL_PHASES = ["design", "plan", "review", "security", "tdd:red", "qa", "verify", "doc"]


def _compose_kwargs(**overrides):
    """Minimal valid compose_briefing kwargs (real on-disk rules), transport pinned
    to CLI so _CLI_TRANSPORT_RULE is always present (the tail-order anchor)."""
    rules = (REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md").read_text(encoding="utf-8")
    assert rules, "rules SKILL.md is empty — fixture broken"
    base = {
        "role_id": "backend-engineer-1",
        "task_id": 7,
        "persona_profile_text": "You are a backend engineer.",
        "phase_procedure_text": "Follow the dev-tdd arc.",
        "task_brief": "Add a unit test for X.",
        "team_id": "atelier-mindiff-team-1",
        "team_lead_name": "team-lead",
        "wave_id": "wave-1",
        "wave_phase": "implement",
        "deadline_iso": "2026-06-06T22:00:00Z",
        "transport": TRANSPORT_CLI,
    }
    base.update(overrides)
    return base


# (1) constant shape — the load-bearing text cannot be silently trimmed away
def test_minimal_diff_rule_shape_and_invariants():
    assert _MINIMAL_DIFF_RULE.startswith("\n\n#")
    assert "YAGNI" in _MINIMAL_DIFF_RULE  # the ladder
    assert "reflex" in _MINIMAL_DIFF_RULE.lower()  # anti-deliberation
    # The FULL safety carve-out must be un-trimmable: pin every load-bearing clause
    # so a future terseness pass that drops any of them turns RED (the carve-out is
    # exactly what keeps ponytail's "100% safe" property).
    for guard in (
        "WHEN NOT TO BE LAZY",
        "trust boundaries",
        "data loss",
        "security",
        "accessibility",
        "EXPLICITLY requested",
        "ONE runnable check",
        "keep the guard",
    ):
        assert guard in _MINIMAL_DIFF_RULE  # safety carve-out


# (2) present for each implementation phase, in the right slot (after terse, before cli)
def test_present_for_implementation_phases_in_order():
    for phase in ("tdd", "tdd:green", "tdd:clean"):
        body = compose_briefing(**_compose_kwargs(wave_phase=phase))
        assert _MINIMAL_DIFF_RULE in body, phase
        # Order invariant: terse < minimal-diff < cli-transport (cli stays the tail).
        assert (
            body.index(_TERSE_OUTPUT_RULE)
            < body.index(_MINIMAL_DIFF_RULE)
            < body.index(_CLI_TRANSPORT_RULE)
        ), phase


# (3) absent for non-impl phases even with the toggle ON (default True)
def test_absent_for_non_implementation_phases():
    for phase in _NON_IMPL_PHASES:
        body = compose_briefing(**_compose_kwargs(wave_phase=phase))
        assert _MINIMAL_DIFF_RULE not in body, phase


# (4) absent when toggled off, even on an implementation phase
def test_absent_when_toggled_off():
    body = compose_briefing(**_compose_kwargs(wave_phase="tdd:green", include_minimal_diff=False))
    assert _MINIMAL_DIFF_RULE not in body


# (5) threads through the production _host_briefing_for via the per-task phase
def test_threads_through_host_briefing_for():
    kw = {"clone_dir": REPO_ROOT, "team_id": "t", "team_lead_name": "lead", "wave_id": "w"}
    impl_task = {"task_id": "AI-X", "assigned_persona": "backend-engineer-1", "phase": "tdd:green"}
    review_task = {"task_id": "AI-Y", "assigned_persona": "security-engineer-1", "phase": "review"}
    assert _MINIMAL_DIFF_RULE in _host_briefing_for(**kw)(impl_task, 1)
    assert _MINIMAL_DIFF_RULE not in _host_briefing_for(**kw, include_minimal_diff=False)(
        impl_task, 1
    )
    # A non-impl phase task is gated out even with the default toggle on.
    assert _MINIMAL_DIFF_RULE not in _host_briefing_for(**kw)(review_task, 1)


# (6) exact-delta neuter guard: ON == OFF with exactly the one rule span excised
def test_on_equals_off_with_rule_excised():
    on = compose_briefing(**_compose_kwargs(wave_phase="tdd:green"))
    off = compose_briefing(**_compose_kwargs(wave_phase="tdd:green", include_minimal_diff=False))
    assert on.count(_MINIMAL_DIFF_RULE) == 1  # exactly one append (no double-append / wrong gate)
    cut = on.find(_MINIMAL_DIFF_RULE)
    assert off == on[:cut] + on[cut + len(_MINIMAL_DIFF_RULE) :]


# (7) existing-parity pin: 'implement' is NOT a gate phase, so the toggle is a no-op.
#     Pins the invariant — a future widening of _IMPLEMENTATION_PHASES to include
#     'implement' must update this test deliberately.
def test_implement_phase_is_not_gated():
    on = compose_briefing(**_compose_kwargs(wave_phase="implement", include_minimal_diff=True))
    off = compose_briefing(**_compose_kwargs(wave_phase="implement", include_minimal_diff=False))
    assert on == off
    assert _MINIMAL_DIFF_RULE not in on
