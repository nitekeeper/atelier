"""Tests for ATELIER_TRANSPORT resolution + the CLI CHANNELS/REPLY-CONTRACT
re-point in compose_briefing.

Since the M7 bridge-queue removal (PR-B) `cli` (the deterministic-host pipeline)
is the ONLY valid transport. The legacy `ATELIER_TRANSPORT=bridge` escape hatch
is GONE — selecting it (or any other value) now fails loud via
`UnknownTransportError`. compose_briefing always appends the CLI addendum.
"""

from __future__ import annotations

import pytest

from scripts.dispatch import (
    TRANSPORT_CLI,
    UnknownTransportError,
    compose_briefing,
    resolve_transport,
)


def test_resolve_transport_defaults_to_cli():
    """An unset / empty / whitespace ATELIER_TRANSPORT resolves to cli (the host
    pipeline — the only transport)."""
    assert resolve_transport(env={}) == TRANSPORT_CLI
    assert resolve_transport(env={"ATELIER_TRANSPORT": ""}) == TRANSPORT_CLI
    assert resolve_transport(env={"ATELIER_TRANSPORT": "  "}) == TRANSPORT_CLI


def test_resolve_transport_bridge_now_raises():
    """The retired `bridge` escape hatch fails loud (the dispatch queue it
    selected was removed in M7) — it must NOT silently fall back to the default.
    The error message names the removal so a stale shell var is diagnosable."""
    with pytest.raises(UnknownTransportError) as exc:
        resolve_transport(env={"ATELIER_TRANSPORT": "bridge"})
    msg = str(exc.value)
    assert "bridge" in msg
    assert "removed in M7" in msg


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


def test_compose_briefing_rejects_bridge_transport():
    """compose_briefing(transport="bridge") fails loud — the bridge transport was
    removed; there is no byte-stable bridge briefing path anymore."""
    with pytest.raises(UnknownTransportError):
        _briefing("bridge")


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
    """compose_briefing(transport=None) resolves the env → cli by default, so an
    existing caller (no transport arg, no env) gets the CLI addendum (the host
    pipeline is the only transport)."""
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


def test_bridge_escape_hatch_env_now_raises_in_compose(monkeypatch):
    """A stale ATELIER_TRANSPORT=bridge in the env fails loud through
    compose_briefing's env resolution (transport=None path), rather than quietly
    selecting a removed transport."""
    monkeypatch.setenv("ATELIER_TRANSPORT", "bridge")
    with pytest.raises(UnknownTransportError):
        compose_briefing(
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
