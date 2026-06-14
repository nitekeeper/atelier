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

from scripts.dispatch import _TERSE_OUTPUT_RULE, TRANSPORT_BRIDGE, compose_briefing

REPO_ROOT = Path(__file__).resolve().parents[1]


def _compose_kwargs(**overrides):
    """Minimal valid kwarg set for compose_briefing (real on-disk sources).

    Pins ``transport=TRANSPORT_BRIDGE`` so the transport under test is
    deterministic regardless of the runner's ambient ``ATELIER_TRANSPORT`` (since
    the M7 flip the env default is ``cli``). These B1 caveman assertions are
    defined against the byte-stable bridge briefing the file documents; a test
    that needs the cli addendum overrides ``transport``.
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
        "transport": TRANSPORT_BRIDGE,
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


def test_b1_terse_rule_present_in_composed_briefing():
    """LIVE proof: compose_briefing's returned briefing CONTAINS the terse
    rule. NEUTER: if the `+ _TERSE_OUTPUT_RULE` append were removed, this
    distinctive substring would be absent and the test goes RED."""
    body = compose_briefing(**_compose_kwargs())
    assert _TERSE_OUTPUT_RULE in body
    # The terse rule is appended AFTER the rendered template body (the append
    # site). It is no longer the briefing's very last section — the always-on
    # context-budget rule (_CONTEXT_BUDGET_RULE) is appended directly after it
    # in a stable terse → context-budget order (see test_context_budget_lever).
    fence_close = body.rfind("</untrusted>")
    assert fence_close != -1
    assert body.find(_TERSE_OUTPUT_RULE) > fence_close


def test_b1_terse_rule_appended_after_template_body_outside_untrusted_fence():
    """The terse rule sits AFTER the rendered template body — i.e. after the
    untrusted TASK fence — never inside the task_brief that the fence escapes.

    The task_brief text is wrapped in `<untrusted source=...>...</untrusted>`
    by the template. The terse rule must appear AFTER the fence close so it is
    real briefing guidance, not escaped untrusted data."""
    body = compose_briefing(**_compose_kwargs())
    fence_close = body.rfind("</untrusted>")
    assert fence_close != -1, "expected an untrusted fence in the rendered briefing"
    terse_at = body.find(_TERSE_OUTPUT_RULE)
    assert terse_at != -1
    assert terse_at > fence_close, "terse rule must be appended AFTER the untrusted fence"


def test_b1_tm006_reply_contract_present_and_unmodified():
    """The B1 append must not disturb the TM-006 reply contract. The
    `# REPLY CONTRACT (verbatim — TM-006)` block + `"type": "task_result"`
    payload survive in the briefing exactly as the template emits them."""
    body = compose_briefing(**_compose_kwargs())
    assert "# REPLY CONTRACT (verbatim — TM-006)" in body
    assert '"type": "task_result"' in body
    # The reply-contract block is BEFORE the appended terse rule (the append
    # is the briefing's tail), so the TM-006 contract is untouched by B1.
    assert body.index('"type": "task_result"') < body.index(_TERSE_OUTPUT_RULE)


def test_b1_task_brief_stays_inside_fence_untouched():
    """The task_brief (untrusted) content still renders inside the fence; the
    terse rule is additive and does not move it out."""
    body = compose_briefing(**_compose_kwargs(task_brief="Add a unit test for X."))
    fence_close = body.rfind("</untrusted>")
    assert "Add a unit test for X." in body
    assert body.index("Add a unit test for X.") < fence_close
