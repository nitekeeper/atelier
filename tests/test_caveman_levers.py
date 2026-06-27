"""Liveness + neuter tests for the context-budget briefing tail and its gate.

The terse/caveman briefing rule (B1) has been fully removed from production —
this module now guards only the appended ``_CONTEXT_BUDGET_RULE`` tail and its
``include_context_budget`` gate, plus a zero-trace regression that the deleted
terse rule never reappears in a composed briefing.

(B2 — the wave-summary digest codec — lives in tests/test_caveman_codec.py and
tests/test_wave_compression.py; it is unrelated to this module.)
"""

from __future__ import annotations

from pathlib import Path

from scripts.dispatch import (
    _CLI_TRANSPORT_RULE,
    _CONTEXT_BUDGET_RULE,
    TRANSPORT_CLI,
    compose_briefing,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _compose_kwargs(**overrides):
    """Minimal valid kwarg set for compose_briefing (real on-disk sources).

    Pins ``transport=TRANSPORT_CLI`` (the only transport since the M7 bridge-queue
    removal) so the transport under test is deterministic regardless of the
    runner's ambient ``ATELIER_TRANSPORT``.
    """
    rules = (REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md").read_text(encoding="utf-8")
    base = {
        "role_id": "backend-engineer-1",
        "task_id": 7,
        "persona_profile_text": "You are a backend engineer.",
        "phase_procedure_text": "Follow the dev-tdd arc.",
        "task_brief": "Add a unit test for X.",
        "team_id": "atelier-caveman-team-1",
        "team_lead_name": "team-lead",
        "wave_id": "wave-1",
        "wave_phase": "implement",
        "deadline_iso": "2026-06-06T22:00:00Z",
        "transport": TRANSPORT_CLI,
    }
    # Sanity: rules fixture is non-empty so the briefing has a body to follow.
    assert rules, "rules SKILL.md is empty — fixture broken"
    base.update(overrides)
    return base


# ── Context-budget tail — LIVE: reaches the rendered briefing, outside the fence ─


def test_context_budget_rule_present_by_default():
    """A normal dispatch carries the context-budget rule, appended AFTER the
    untrusted fence."""
    body = compose_briefing(**_compose_kwargs(wave_phase="tdd:green"))
    assert _CONTEXT_BUDGET_RULE in body
    fence_close = body.rfind("</untrusted>")
    assert fence_close != -1
    assert body.find(_CONTEXT_BUDGET_RULE) > fence_close


def test_tm006_reply_contract_present_and_precedes_budget_tail():
    """The TM-006 reply contract survives and precedes the appended budget tail."""
    body = compose_briefing(**_compose_kwargs(wave_phase="tdd:green"))
    assert "# REPLY CONTRACT (verbatim — TM-006)" in body
    assert '"type": "task_result"' in body
    assert body.index('"type": "task_result"') < body.index(_CONTEXT_BUDGET_RULE)


def test_task_brief_stays_inside_fence_untouched():
    """The untrusted task_brief renders inside the fence."""
    body = compose_briefing(**_compose_kwargs(task_brief="Add a unit test for X."))
    fence_close = body.rfind("</untrusted>")
    assert "Add a unit test for X." in body
    assert body.index("Add a unit test for X.") < fence_close


# ── include_context_budget gate ───────────────────────────────────────────


def test_include_context_budget_false_omits_budget_tail_only():
    """include_context_budget=False drops the appended context-budget rule but NOT
    the CLI transport addendum."""
    off = compose_briefing(**_compose_kwargs(include_context_budget=False))
    assert _CONTEXT_BUDGET_RULE not in off
    assert _CLI_TRANSPORT_RULE in off


def test_include_context_budget_byte_parity_and_single_sourcing():
    """explicit include_context_budget=True == implicit default. The flag now
    SINGLE-SOURCES the context-budget guidance (no duplication, never wholly
    dropped): True appends the _CONTEXT_BUDGET_RULE tail AND strips the duplicate
    rules-block "## Context-budget discipline" subsection; False keeps the
    rules-block subsection and omits the tail. Toggling moves the guidance between
    the two sites — it appears EXACTLY ONCE either way (anti-recoupling guard)."""
    explicit = compose_briefing(**_compose_kwargs(include_context_budget=True))
    on = compose_briefing(**_compose_kwargs())
    assert explicit == on
    # True: appended tail present (exactly once), rules-block duplicate stripped.
    assert _CONTEXT_BUDGET_RULE in on
    assert on.count(_CONTEXT_BUDGET_RULE) == 1
    assert "## Context-budget discipline (reference)" not in on
    # False: no appended tail, but the rules-block subsection is retained — so the
    # guidance is never lost, only relocated. Each path single-sources it.
    off = compose_briefing(**_compose_kwargs(include_context_budget=False))
    assert _CONTEXT_BUDGET_RULE not in off
    assert "## Context-budget discipline (reference)" in off


def test_include_context_budget_threads_through_host_briefing_for():
    """include_context_budget propagates through cli_dispatch._host_briefing_for;
    False drops the budget tail."""
    from scripts.cli_dispatch import _host_briefing_for

    task = {"task_id": "AI-X", "assigned_persona": "backend-engineer-1", "phase": "tdd:green"}
    kw = {"clone_dir": REPO_ROOT, "team_id": "t", "team_lead_name": "lead", "wave_id": "w"}
    on = _host_briefing_for(**kw)(task, 1)
    off = _host_briefing_for(**kw, include_context_budget=False)(task, 1)
    assert _CONTEXT_BUDGET_RULE in on
    assert _CONTEXT_BUDGET_RULE not in off


def test_include_context_budget_false_still_carries_rules_block_budget():
    """include_context_budget=False drops the APPENDED _CONTEXT_BUDGET_RULE constant,
    but the equivalent discipline in the always-rendered team-mode-rules block
    survives."""
    off = compose_briefing(**_compose_kwargs(include_context_budget=False))
    assert _CONTEXT_BUDGET_RULE not in off
    assert "accumulating past" in off


# ── B1 zero-trace regression — the terse rule must NEVER reappear ──────────


def test_terse_rule_fully_removed_zero_trace(monkeypatch):
    """The deleted terse/caveman briefing rule (B1) must never appear in a composed
    briefing — not even with the retired ATELIER_INCLUDE_TERSE=1 env set (proves the
    env is dead and the rule left zero trace)."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "1")
    # Neutral team_id so the fixture's own name does not contribute a false "caveman"
    # hit — the assertions key off the deleted B1 rule's distinctive prose.
    body = compose_briefing(
        **_compose_kwargs(wave_phase="tdd:green", team_id="atelier-zerotrace-1")
    )
    low = body.lower()
    assert "# output shape (terse" not in low
    assert "smart caveman" not in low
    assert "talk like a smart caveman" not in low
    assert "only fluff dies" not in low
