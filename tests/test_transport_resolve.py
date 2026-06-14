"""Tests for ATELIER_TRANSPORT resolution + the CLI CHANNELS/REPLY-CONTRACT
re-point in compose_briefing (M3 deliverable #2).

Since M7 the default transport is `cli` (the deterministic-host pipeline);
`ATELIER_TRANSPORT=bridge` is the explicit escape hatch. The bridge briefing is
BYTE-STABLE when bridge is selected (explicitly or via the escape hatch) — the
CLI addendum is appended in `cli` transport, leaving the bridge path untouched.
"""

from __future__ import annotations

import pytest

from scripts.dispatch import (
    TRANSPORT_BRIDGE,
    TRANSPORT_CLI,
    UnknownTransportError,
    compose_briefing,
    resolve_transport,
)


def test_resolve_transport_defaults_to_cli():
    """M7 flip: an unset / empty / whitespace ATELIER_TRANSPORT now resolves to
    cli (the host pipeline default)."""
    assert resolve_transport(env={}) == TRANSPORT_CLI
    assert resolve_transport(env={"ATELIER_TRANSPORT": ""}) == TRANSPORT_CLI
    assert resolve_transport(env={"ATELIER_TRANSPORT": "  "}) == TRANSPORT_CLI


def test_resolve_transport_bridge_escape_hatch():
    """ATELIER_TRANSPORT=bridge is the explicit escape hatch back to the legacy
    bridge path (highest-precedence env override, survives the M7 default flip)."""
    assert resolve_transport(env={"ATELIER_TRANSPORT": "bridge"}) == TRANSPORT_BRIDGE


def test_resolve_transport_cli_opt_in():
    assert resolve_transport(env={"ATELIER_TRANSPORT": "cli"}) == TRANSPORT_CLI


def test_resolve_transport_rejects_unknown():
    with pytest.raises(UnknownTransportError):
        resolve_transport(env={"ATELIER_TRANSPORT": "grpc"})


def _briefing(transport):
    return compose_briefing(
        role_id="be-1",
        task_id="t-1",
        persona_profile_text="PERSONA",
        phase_procedure_text="PHASE",
        task_brief="do the thing",
        team_id="team-x",
        team_lead_name="lead",
        wave_id="wave-0",
        wave_phase="tdd:green",
        deadline_iso="2026-06-13T00:00:00Z",
        transport=transport,
    )


def test_bridge_briefing_has_no_cli_addendum():
    """The bridge briefing does NOT carry the CLI transport-override block — the
    template's bridge CHANNELS block stands."""
    b = _briefing(TRANSPORT_BRIDGE)
    assert "TRANSPORT OVERRIDE — CLI MODE" not in b
    # The bridge CHANNELS wiring is present (the template's bridge block).
    assert "# CHANNELS" in b


def test_cli_briefing_appends_repoint_addendum():
    """The cli briefing appends the CHANNELS/REPLY-CONTRACT re-point: ignore
    bridge_send.py, return the structured final message matching the schema."""
    b = _briefing(TRANSPORT_CLI)
    assert "TRANSPORT OVERRIDE — CLI MODE" in b
    assert "structured final message matching the provided json-schema" in b
    # It explicitly re-points away from the bridge commands.
    assert "bridge_send.py" in b  # named so the worker knows to IGNORE it
    assert "IGNORE every" in b


def test_cli_is_the_default_when_transport_unset(monkeypatch):
    """M7 flip: compose_briefing(transport=None) resolves the env → cli by
    default, so an existing caller (no transport arg, no env) now gets the CLI
    addendum (the host pipeline is the default transport)."""
    monkeypatch.delenv("ATELIER_TRANSPORT", raising=False)
    b = compose_briefing(
        role_id="be-1",
        task_id="t-1",
        persona_profile_text="P",
        phase_procedure_text="PH",
        task_brief="x",
        team_id="t",
        team_lead_name="l",
        wave_id="w",
        wave_phase="tdd:green",
        deadline_iso="2026-06-13T00:00:00Z",
    )
    assert "TRANSPORT OVERRIDE — CLI MODE" in b


def test_bridge_escape_hatch_briefing_is_byte_stable(monkeypatch):
    """ATELIER_TRANSPORT=bridge (the escape hatch) yields the byte-stable bridge
    briefing with NO CLI addendum, even after the M7 default flip."""
    monkeypatch.setenv("ATELIER_TRANSPORT", "bridge")
    b = compose_briefing(
        role_id="be-1",
        task_id="t-1",
        persona_profile_text="P",
        phase_procedure_text="PH",
        task_brief="x",
        team_id="t",
        team_lead_name="l",
        wave_id="w",
        wave_phase="tdd:green",
        deadline_iso="2026-06-13T00:00:00Z",
    )
    assert "TRANSPORT OVERRIDE — CLI MODE" not in b


def test_cli_transport_via_env(monkeypatch):
    """With ATELIER_TRANSPORT=cli in the env and no explicit arg, compose_briefing
    appends the CLI addendum."""
    monkeypatch.setenv("ATELIER_TRANSPORT", "cli")
    b = compose_briefing(
        role_id="be-1",
        task_id="t-1",
        persona_profile_text="P",
        phase_procedure_text="PH",
        task_brief="x",
        team_id="t",
        team_lead_name="l",
        wave_id="w",
        wave_phase="tdd:green",
        deadline_iso="2026-06-13T00:00:00Z",
    )
    assert "TRANSPORT OVERRIDE — CLI MODE" in b
