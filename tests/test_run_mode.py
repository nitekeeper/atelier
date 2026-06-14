"""Tests for ``scripts.run_mode`` — the M6b-2 R-MODE per-run cost/quality posture.

Covers Iron-Law 2 (resolution precedence + non-interactive/CI auto-default WITHOUT
a blocking prompt + the PROFILES-unification mapping) and Iron-Law 3's
balanced-present / global-flow-handles-3 obligation. Pure unit tests: no disk I/O,
no settings.json write, injectable ``env``.
"""

from __future__ import annotations

import pytest

from scripts import recommended_settings as rs
from scripts import run_mode as rm
from scripts.run_mode import (
    BALANCED,
    COST_LEAN,
    ENV_RUN_MODE_VAR,
    QUALITY_LEAN,
    RunMode,
    resolve_run_mode,
)

# ── M6b-2 Iron-Law 2: precedence + CI no-block + PROFILES unification ─────────


def test_resolve_run_mode_precedence_and_saved_default(monkeypatch):
    """explicit > interactive_choice > env ATELIER_RUN_MODE > saved-profile default;
    the mapping REUSES recommended_settings.PROFILES (not a forked model list).

    FIX 4 reframe: ``resolve_run_mode`` does NO I/O — it CANNOT block (so there is
    no "CI no-block" behavior to assert here; that prompt-vs-silent gate lives in the
    SKILL prose, not this pure function). This test pins the precedence rungs + the
    saved-profile fallback + the PROFILES unification — exactly what the function
    guarantees. A stray ``CI`` env key is irrelevant to resolution (proven below).

    RED pre-fix: ``scripts.run_mode`` does not exist → ImportError.
    """
    monkeypatch.delenv(ENV_RUN_MODE_VAR, raising=False)
    no_pin = {}  # no ATELIER_RUN_MODE → the saved-profile default fires

    # Rung 1 — explicit wins over EVERYTHING (interactive answer + env pin both set).
    rmode = resolve_run_mode(
        explicit=QUALITY_LEAN,
        interactive_choice=COST_LEAN,
        env={ENV_RUN_MODE_VAR: BALANCED},
    )
    assert rmode.mode_id == QUALITY_LEAN

    # Rung 2 — interactive_choice wins over the env pin (no explicit).
    rmode = resolve_run_mode(interactive_choice=BALANCED, env={ENV_RUN_MODE_VAR: COST_LEAN})
    assert rmode.mode_id == BALANCED

    # Rung 3 — env ATELIER_RUN_MODE wins (no explicit, no interactive answer).
    rmode = resolve_run_mode(env={ENV_RUN_MODE_VAR: QUALITY_LEAN})
    assert rmode.mode_id == QUALITY_LEAN

    # Rung 4 — no higher rung supplied → the SAVED profile's mode
    # (DEFAULT_PROFILE → default_mode_id). Returns silently (no I/O, no prompt).
    rmode = resolve_run_mode(env=no_pin)
    assert rmode.profile_id == rs.DEFAULT_PROFILE
    assert rmode.mode_id == rm.default_mode_id()
    # A stray CI marker is IRRELEVANT — resolution reads only ATELIER_RUN_MODE; the
    # CI/TTY detection lives in the SKILL, not this pure function. Same outcome.
    assert resolve_run_mode(env={"CI": "true", "GITHUB_ACTIONS": "true"}).mode_id == rmode.mode_id

    # An invalid value at any rung is IGNORED (falls through), never raises.
    rmode = resolve_run_mode(explicit="garbage", interactive_choice="garbage", env=no_pin)
    assert rmode.profile_id == rs.DEFAULT_PROFILE

    # PROFILES UNIFICATION: every mode's orchestrator model is READ from PROFILES —
    # NOT a forked list. Assert each mode's orchestrator_model == PROFILES[profile].
    for mode_id in (COST_LEAN, BALANCED, QUALITY_LEAN):
        rmode = resolve_run_mode(explicit=mode_id, env=no_pin)
        assert rmode.orchestrator_model == rs.PROFILES[rmode.profile_id]["model"], (
            "orchestrator model MUST be sourced from recommended_settings.PROFILES "
            "(single source — no forked model list)"
        )
    # And the three modes map onto the three profiles per the documented table.
    assert resolve_run_mode(explicit=COST_LEAN, env=no_pin).profile_id == "cost-effective"
    assert resolve_run_mode(explicit=BALANCED, env=no_pin).profile_id == "balanced"
    assert resolve_run_mode(explicit=QUALITY_LEAN, env=no_pin).profile_id == "code-quality"


def test_resolve_run_mode_is_pure_no_io(monkeypatch):
    """FIX 4 — ``resolve_run_mode`` does NO I/O: it never reads stdin / a TTY and
    never writes anything. It reads ONLY the injected env (+ PROFILES). Proven by
    forcing ``sys.stdin.isatty`` to raise — resolution is unaffected (it never calls
    it), so a closed/detached stdin can never make it block or crash."""
    import sys

    def _boom():
        raise AssertionError("resolve_run_mode must NOT touch sys.stdin")

    monkeypatch.setattr(sys.stdin, "isatty", _boom, raising=False)
    monkeypatch.delenv(ENV_RUN_MODE_VAR, raising=False)
    # No stdin touch ⇒ no raise; resolves to the saved-profile default silently.
    assert resolve_run_mode(env={}).profile_id == rs.DEFAULT_PROFILE
    assert resolve_run_mode(explicit=QUALITY_LEAN, env={}).mode_id == QUALITY_LEAN


def test_resolve_run_mode_posture_mapping(monkeypatch):
    """The posture per mode: cost-lean→cost-lean, balanced→neutral,
    quality-lean→opus-lean. balanced is the NEUTRAL identity."""
    monkeypatch.delenv(ENV_RUN_MODE_VAR, raising=False)
    ci = {"CI": "1"}
    assert resolve_run_mode(explicit=COST_LEAN, env=ci).posture == "cost-lean"
    assert resolve_run_mode(explicit=BALANCED, env=ci).posture == "neutral"
    assert resolve_run_mode(explicit=QUALITY_LEAN, env=ci).posture == "opus-lean"
    # balanced is the no-op identity across ALL levers.
    assert resolve_run_mode(explicit=BALANCED, env=ci).is_neutral is True
    # cost-lean / quality-lean are NON-neutral (they move at least one lever).
    assert resolve_run_mode(explicit=COST_LEAN, env=ci).is_neutral is False
    assert resolve_run_mode(explicit=QUALITY_LEAN, env=ci).is_neutral is False


def test_run_mode_budget_total_scaling():
    """budget_total_for scales the base total by the mode's ceiling factor; balanced
    is the identity (factor 1.0). Floors + clamps >= 1."""
    balanced = resolve_run_mode(explicit=BALANCED, env={"CI": "1"})
    assert balanced.budget_total_for(1_000_000) == 1_000_000  # identity
    cost = resolve_run_mode(explicit=COST_LEAN, env={"CI": "1"})
    quality = resolve_run_mode(explicit=QUALITY_LEAN, env={"CI": "1"})
    # cost-lean shrinks, quality-lean grows the pool.
    assert cost.budget_total_for(1_000_000) < 1_000_000
    assert quality.budget_total_for(1_000_000) > 1_000_000
    # tiny base never produces a non-positive total (BudgetPool would raise).
    assert cost.budget_total_for(1) >= 1


def test_run_mode_is_frozen_value_object():
    """RunMode is an immutable frozen dataclass (a per-run posture is a value)."""
    rmode = resolve_run_mode(explicit=BALANCED, env={"CI": "1"})
    assert isinstance(rmode, RunMode)
    with pytest.raises((AttributeError, TypeError)):
        rmode.mode_id = "mutated"  # type: ignore[misc]
