"""Anti-revert tests for the context-budget discipline lever (cycle 2).

The user's measured pain: subagents frequently blow a single task past ~150k
tokens. The atelier PostToolUse 125k nudge (``hooks/context_budget.py``) and
PreCompact snapshot (``hooks/pre_compact.py``) fire ONLY in the orchestrator's
interactive session (scoped to ``.ai/active_project``) — they do NOT reach a
one-shot worker spawned by the host dispatch pipeline. So the ONLY
context-budget signal that reaches a worker is its BRIEFING:

* AI-1 — the always-on ``_CONTEXT_BUDGET_RULE`` appended by
  ``scripts/dispatch.py::compose_briefing`` (the team-mode worker path).
* AI-2 — the ``## Context budget`` section in the three
  ``internal/dev-subagent/*-prompt.md`` templates (the dev-arc sub-agent path,
  which substitutes ``{{vars}}`` rather than going through ``compose_briefing``).
* AI-3 — the orchestrator-vs-subagent scope boundary documented in
  ``internal/team-mode-rules/SKILL.md`` (an internal procedure file — cycle
  agents may edit it, unlike the governance charter ``CLAUDE.md``, which atelier's
  A-rules forbid cycle agents from touching outside a governance-scoped run) so a
  future cycle does not re-assume the hooks reach subagents.

Each assertion goes RED on a silent revert of the corresponding enforcement —
the kaizen "shipped inert" failure mode (levers that save zero tokens / guidance
silently dropped) is guarded against here, mirroring
``tests/test_caveman_levers.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.dispatch import (
    _CLI_TRANSPORT_RULE,
    _CONTEXT_BUDGET_RULE,
    TRANSPORT_CLI,
    compose_briefing,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_DEV_SUBAGENT_PROMPTS = (
    "implementer-prompt.md",
    "spec-reviewer-prompt.md",
    "quality-reviewer-prompt.md",
)


def _compose_kwargs(**overrides):
    """Minimal valid kwarg set for compose_briefing (real on-disk sources).

    Pins ``transport=TRANSPORT_CLI`` (the only transport since the M7 bridge-queue
    removal) so the AI-1 budget-rule ORDER invariants hold deterministically
    regardless of the runner's ambient ``ATELIER_TRANSPORT``. On the cli path the
    order is context-budget → cli-transport addendum, so the budget rule is NOT the
    literal tail (the cli addendum is) — the ORDER assertions below key off relative
    position, not the tail.
    """
    rules = (REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md").read_text(encoding="utf-8")
    base = {
        "role_id": "backend-engineer-1",
        "task_id": 7,
        "persona_profile_text": "You are a backend engineer.",
        "phase_procedure_text": "Follow the dev-tdd arc.",
        "task_brief": "Add a unit test for X.",
        "team_id": "atelier-ctxbudget-team-1",
        "team_lead_name": "team-lead",
        "wave_id": "wave-1",
        "wave_phase": "implement",
        "deadline_iso": "2026-06-07T22:00:00Z",
        "transport": TRANSPORT_CLI,
    }
    assert rules, "rules SKILL.md is empty — fixture broken"
    base.update(overrides)
    return base


# ── AI-1 — constant shape (neuter backstop) ───────────────────────────────


def test_ai1_context_budget_rule_is_nonempty_constant():
    """Silent blanking of the constant turns the liveness assertions RED."""
    assert isinstance(_CONTEXT_BUDGET_RULE, str)
    assert _CONTEXT_BUDGET_RULE.strip()
    # Load-bearing tokens the action item names.
    assert "125" in _CONTEXT_BUDGET_RULE
    low = _CONTEXT_BUDGET_RULE.lower()
    assert "checkpoint" in low
    # A wind-down / return phrase (the worker must terminate, not keep going).
    assert "wind down" in low
    assert "return" in low
    # HONEST mechanism — must NOT claim silent/automatic compaction.
    assert "silent" not in low or "cannot silently" in low
    # It must explicitly state the agent acts ("you must" / "your responsibility").
    assert "you must act" in low or "your responsibility" in low


def test_ai1_context_budget_rule_does_not_claim_silent_automation():
    """Honesty guard: the rule must not assert that atelier silently/auto
    compacts the subagent — Claude Code has no such trigger. Any use of the
    word 'silent' must be in the NEGATED ('cannot silently') sense."""
    low = _CONTEXT_BUDGET_RULE.lower()
    for marker in ("silently auto-compact", "automatic compaction", "auto-compact"):
        if marker in low:
            # Allowed only in an explicit negation.
            idx = low.find(marker)
            window = low[max(0, idx - 24) : idx]
            assert "cannot" in window or "no " in window or "not " in window, (
                f"'{marker}' must appear only in a negated/honest context"
            )


# ── AI-1 — LIVE: the rule reaches the rendered briefing, outside the fence ──


def test_ai1_context_budget_rule_present_in_cli_default_briefing():
    """LIVE proof on the CLI/host transport — the M7 PRODUCTION DEFAULT (and, since
    the bridge-queue removal, the ONLY transport). Asserts the rule survives and
    its position is the STABLE context-budget → cli-transport order. The cli branch
    appends `_CLI_TRANSPORT_RULE` after the budget rule, so the budget rule is NOT
    the literal tail. NEUTER: dropping the `+ _CONTEXT_BUDGET_RULE` append makes
    this RED."""
    body = compose_briefing(**_compose_kwargs(transport=TRANSPORT_CLI))
    # (a) the budget rule reaches the cli-default briefing.
    assert _CONTEXT_BUDGET_RULE in body
    # (b) stable order: context-budget < cli-transport addendum (the cli addendum
    #     is the tail on this path, so the budget rule is NOT the tail).
    budget_at = body.find(_CONTEXT_BUDGET_RULE)
    cli_at = body.find(_CLI_TRANSPORT_RULE)
    assert budget_at != -1 and cli_at != -1
    assert budget_at < cli_at, "cli briefing order must be context-budget → cli-transport addendum"


def test_ai1_context_budget_rule_present_under_cli_env_default(monkeypatch):
    """The literal env-default path: with ATELIER_TRANSPORT unset, compose_briefing
    resolves the M7 default (cli) and STILL carries the budget rule (the lever is
    not env-default-flaky on the production default)."""
    monkeypatch.delenv("ATELIER_TRANSPORT", raising=False)
    # No transport arg → compose_briefing resolves the env default (cli since M7).
    kwargs = _compose_kwargs()
    kwargs.pop("transport")
    body = compose_briefing(**kwargs)
    assert _CONTEXT_BUDGET_RULE in body
    # The cli addendum is present (proving the env default resolved to cli, not a
    # stale bridge default).
    assert _CLI_TRANSPORT_RULE in body


def test_ai1_budget_rule_after_untrusted_fence():
    """The budget rule sits AFTER the untrusted TASK fence (guidance, not
    injectable task data)."""
    body = compose_briefing(**_compose_kwargs())
    fence_close = body.rfind("</untrusted>")
    assert fence_close != -1, "expected an untrusted fence in the rendered briefing"
    budget_at = body.find(_CONTEXT_BUDGET_RULE)
    assert budget_at != -1
    assert budget_at > fence_close, "budget rule must be appended AFTER the untrusted fence"


def test_ai1_tm006_reply_contract_present_and_precedes_budget_rule():
    """The B1/AI-1 append must not disturb the TM-006 reply contract: the
    ``"type": "task_result"`` payload survives and appears BEFORE the appended
    budget rule (the envelope is unmodified by the tail appends)."""
    body = compose_briefing(**_compose_kwargs())
    assert '"type": "task_result"' in body
    assert body.index('"type": "task_result"') < body.index(_CONTEXT_BUDGET_RULE)


# ── AI-2 — the dev-subagent prompt templates carry the discipline ──────────


def test_ai2_dev_subagent_prompts_carry_context_budget_section():
    """Each of the three dev-subagent prompt templates contains the
    context-budget guidance. RED if any template silently drops the section."""
    base = REPO_ROOT / "internal" / "dev-subagent"
    for name in _DEV_SUBAGENT_PROMPTS:
        text = (base / name).read_text(encoding="utf-8")
        low = text.lower()
        assert "## context budget" in low, f"{name} missing '## Context budget' section"
        assert "125" in text, f"{name} missing the 125k threshold"
        assert "checkpoint" in low or "wind down" in low, (
            f"{name} missing checkpoint / wind-down guidance"
        )


def test_ai2_dev_subagent_section_introduces_no_new_placeholder():
    """The added prose must not introduce a NEW ``{{placeholder}}`` the caller
    (internal/dev-subagent/SKILL.md) does not already substitute. We assert the
    ``## Context budget`` section text contains no ``{{`` token at all."""
    placeholder_re = re.compile(r"\{\{")
    base = REPO_ROOT / "internal" / "dev-subagent"
    for name in _DEV_SUBAGENT_PROMPTS:
        text = (base / name).read_text(encoding="utf-8")
        idx = text.lower().find("## context budget")
        assert idx != -1, f"{name} missing the section"
        section = text[idx:]
        assert not placeholder_re.search(section), (
            f"{name}'s Context budget section introduced a new {{{{placeholder}}}}"
        )


# ── AI-3 — the scope boundary is documented (orchestrator-only hooks) ──────


def test_ai3_team_mode_rules_documents_orchestrator_only_scope():
    """internal/team-mode-rules/SKILL.md (an internal procedure file — editable by
    cycle agents, unlike the governance charter CLAUDE.md, which atelier's A-rules
    forbid cycle agents from modifying outside a governance-scoped run) states the
    hooks are orchestrator-session-only, scoped to ``active_project``, with the
    ~125k threshold — so a future cycle does not re-assume the hooks reach
    subagents."""
    text = (REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md").read_text(encoding="utf-8")
    low = text.lower()
    # Stable sentinel subsection added by this cycle. Target the SECTION header
    # ("## context-budget discipline"), not the earlier CHANGELOG mention of the
    # same phrase, so the proximity window below lands on the real prose.
    assert "## context-budget discipline" in low
    # Orchestrator-only scope, the active_project gate, and the threshold appear
    # in proximity (within the same documented note).
    idx = low.find("## context-budget discipline")
    window = low[idx : idx + 2500]
    assert "orchestrator" in window
    assert "active_project" in window
    assert "125" in window
    # The honest mechanism: subagents do NOT inherit / are not reached by the hooks.
    assert (
        "do not reach you" in window or "do not inherit" in window or "does not inherit" in window
    )


def test_ai3_team_mode_rules_changelog_grew_and_schema_unchanged():
    """team-mode-rules/SKILL.md gained a CHANGELOG row for this cycle AND its
    schema_version is UNCHANGED (still 1) — the doc-only invariant. RED if the
    row is dropped or schema_version is bumped without a migration."""
    text = (REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md").read_text(encoding="utf-8")
    # schema_version frontmatter still 1 (doc-only change — no DB schema bump).
    m = re.search(r"^schema_version:\s*(\d+)\s*$", text, re.MULTILINE)
    assert m is not None, "schema_version frontmatter missing"
    assert m.group(1) == "1", "schema_version must stay 1 for a doc-only change"
    # The new CHANGELOG row + the reference subsection exist.
    assert "| 1.3.1" in text, "expected a new 1.3.1 CHANGELOG row"
    assert "## Context-budget discipline (reference)" in text
    low = text.lower()
    assert "125000" in text and "checkpoint" in low
