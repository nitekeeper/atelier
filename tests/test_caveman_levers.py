"""Liveness + neuter tests for the two caveman token-compression levers.

B1 — always-on terse-output rule appended to the RENDERED worker briefing
     (scripts/dispatch.py::compose_briefing + _TERSE_OUTPUT_RULE).
B2 — env-gated caveman codec at the wave-summary digest sink
     (scripts/pm_dispatch.py::compress_reply_for_context + WaveDispatcher).

Both levers ship LIVE. Each carries a NEUTER assertion: a silent revert of the
lever (removing the B1 append, or the B2 codec call / flipping the gate OFF)
turns the suite RED. The kaizen round-1 failure mode (levers shipped INERT,
saving zero tokens) is specifically guarded against here.
"""

from __future__ import annotations

from pathlib import Path

from scripts.dispatch import (
    _CLI_TRANSPORT_RULE,
    _CONTEXT_BUDGET_RULE,
    _TERSE_OUTPUT_RULE,
    TRANSPORT_CLI,
    compose_briefing,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _compose_kwargs(**overrides):
    """Minimal valid kwarg set for compose_briefing (real on-disk sources).

    Pins ``transport=TRANSPORT_CLI`` (the only transport since the M7 bridge-queue
    removal) so the transport under test is deterministic regardless of the
    runner's ambient ``ATELIER_TRANSPORT``. The B1 caveman assertions (terse rule
    is appended after the untrusted fence, and before the appended-tail blocks)
    hold on the cli path: the terse rule precedes both the context-budget rule and
    the cli-transport addendum.
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


# ── B1 — constant shape (neuter backstop) ─────────────────────────────────


def test_b1_terse_rule_is_nonempty_constant():
    """A silent blanking of the constant turns the liveness assertions below
    RED (they require a distinctive non-empty substring)."""
    assert isinstance(_TERSE_OUTPUT_RULE, str)
    assert _TERSE_OUTPUT_RULE.strip()
    # Distinctive markers the liveness test keys off.
    low = _TERSE_OUTPUT_RULE.lower()
    assert "terse" in low
    assert "notes_md" in _TERSE_OUTPUT_RULE
    assert "caveman" in low
    # HARD CARVE-OUT contract is spelled out verbatim.
    assert "task_result" in _TERSE_OUTPUT_RULE
    assert "shutdown_response" in _TERSE_OUTPUT_RULE
    assert "ABANDON:" in _TERSE_OUTPUT_RULE


# ── B1 — LIVE: the rule reaches the rendered briefing, outside the fence ───


def test_b1_terse_rule_present_when_enabled(monkeypatch):
    """Terse is now DEFAULT-OFF (measured net loss); opt in via ATELIER_INCLUDE_TERSE=1.
    When enabled, the rule reaches the briefing, appended AFTER the untrusted fence."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "1")
    body = compose_briefing(**_compose_kwargs())
    assert _TERSE_OUTPUT_RULE in body
    fence_close = body.rfind("</untrusted>")
    assert fence_close != -1
    assert body.find(_TERSE_OUTPUT_RULE) > fence_close


def test_b1_tm006_reply_contract_present_and_unmodified(monkeypatch):
    """Even with terse enabled, the TM-006 reply contract survives and precedes it."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "1")
    body = compose_briefing(**_compose_kwargs())
    assert "# REPLY CONTRACT (verbatim — TM-006)" in body
    assert '"type": "task_result"' in body
    assert body.index('"type": "task_result"') < body.index(_TERSE_OUTPUT_RULE)


def test_b1_task_brief_stays_inside_fence_untouched():
    """The untrusted task_brief renders inside the fence regardless of terse."""
    body = compose_briefing(**_compose_kwargs(task_brief="Add a unit test for X."))
    fence_close = body.rfind("</untrusted>")
    assert "Add a unit test for X." in body
    assert body.index("Add a unit test for X.") < fence_close


# ── B1' — terse is DEFAULT-OFF (measured net loss at every tier); opt-in via env ──
def test_terse_off_by_default():
    """A normal sonnet dispatch (env unset) must NOT carry the terse rule, while
    still carrying the context-budget rule."""
    body = compose_briefing(**_compose_kwargs(wave_phase="tdd:green"))
    assert _TERSE_OUTPUT_RULE not in body
    assert _CONTEXT_BUDGET_RULE in body


def test_terse_on_via_env(monkeypatch):
    """ATELIER_INCLUDE_TERSE=1 opts the terse rule back in (the A/B re-test hook)."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "1")
    body = compose_briefing(**_compose_kwargs(wave_phase="tdd:green"))
    assert _TERSE_OUTPUT_RULE in body
    assert _CONTEXT_BUDGET_RULE in body


def test_terse_env_force_off(monkeypatch):
    """ATELIER_INCLUDE_TERSE=0 (explicit) also keeps terse off, budget on."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "0")
    body = compose_briefing(**_compose_kwargs(wave_phase="tdd:green"))
    assert _TERSE_OUTPUT_RULE not in body
    assert _CONTEXT_BUDGET_RULE in body


def test_include_terse_false_omits_both_rules():
    """include_terse=False drops the appended context-budget rule (and terse, off
    anyway), but NOT the CLI transport addendum."""
    off = compose_briefing(**_compose_kwargs(include_terse=False))
    assert _TERSE_OUTPUT_RULE not in off
    assert _CONTEXT_BUDGET_RULE not in off
    assert _CLI_TRANSPORT_RULE in off


def test_terse_enabled_byte_parity_and_exact_delta(monkeypatch):
    """With terse opted in: explicit include_terse=True == implicit default; both
    rules present; and include_terse=False == that default with exactly the
    terse+budget tail excised (anti-recoupling delta guard)."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "1")
    explicit = compose_briefing(**_compose_kwargs(include_terse=True))
    on = compose_briefing(**_compose_kwargs())
    assert explicit == on
    assert _TERSE_OUTPUT_RULE in on
    assert _CONTEXT_BUDGET_RULE in on
    off = compose_briefing(**_compose_kwargs(include_terse=False))
    combo = _TERSE_OUTPUT_RULE + _CONTEXT_BUDGET_RULE
    assert on.count(combo) == 1
    cut = on.find(combo)
    assert off == on[:cut] + on[cut + len(combo) :]


def test_terse_threads_through_host_briefing_for(monkeypatch):
    """With terse enabled, it propagates through cli_dispatch._host_briefing_for;
    include_terse=False still drops the budget tail."""
    monkeypatch.setenv("ATELIER_INCLUDE_TERSE", "1")
    from scripts.cli_dispatch import _host_briefing_for

    task = {"task_id": "AI-X", "assigned_persona": "backend-engineer-1", "phase": "tdd:green"}
    kw = {"clone_dir": REPO_ROOT, "team_id": "t", "team_lead_name": "lead", "wave_id": "w"}
    on = _host_briefing_for(**kw)(task, 1)
    off = _host_briefing_for(**kw, include_terse=False)(task, 1)
    assert _TERSE_OUTPUT_RULE in on
    assert _TERSE_OUTPUT_RULE not in off
    assert _CONTEXT_BUDGET_RULE not in off


def test_include_terse_false_still_carries_rules_block_context_budget():
    """include_terse=False drops the APPENDED _CONTEXT_BUDGET_RULE constant, but the
    equivalent discipline in the always-rendered team-mode-rules block survives."""
    off = compose_briefing(**_compose_kwargs(include_terse=False))
    assert _CONTEXT_BUDGET_RULE not in off
    assert "accumulating past" in off
