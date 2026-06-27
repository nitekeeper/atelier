"""Tests for ATELIER_TRANSPORT resolution + the CLI CHANNELS/REPLY-CONTRACT
re-point in compose_briefing.

Since the M7 bridge-queue removal (PR-B) `cli` (the deterministic-host pipeline)
is the ONLY valid transport. The legacy `ATELIER_TRANSPORT=bridge` escape hatch
is GONE — selecting it (or any other value) now fails loud via
`UnknownTransportError`. compose_briefing always appends the CLI addendum.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import scripts.dispatch as dispatch
from scripts.dispatch import (
    ROLE_TEMPLATE,
    RULES_SKILL,
    TEMPLATE_DIR,
    TRANSPORT_CLI,
    UnknownTransportError,
    compose_briefing,
    resolve_transport,
)
from scripts.loom_comms import LoomStatus, build_team_chat_context

_ROLE_J2 = TEMPLATE_DIR / ROLE_TEMPLATE


def _cli_briefing(*, team_chat=None):
    """A cli compose with the same placeholder context as ``_briefing`` but an
    optional loom ``team_chat`` (for the loom-on carveout assertions)."""
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
        transport=TRANSPORT_CLI,
        team_chat=team_chat,
    )


def _loom_ctx():
    return build_team_chat_context(
        LoomStatus(available=True, client=Path("/fake/loom_chat.py")),
        role_id="be-1",
        channel="cycle-x",
        team_lead_name="lead",
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
    # The cli inert-protocol strip means the shrunk addendum no longer names the
    # bridge commands (the whole CHANNELS bridge wire is stripped, so there is
    # nothing to tell the worker to IGNORE).
    assert "bridge_send.py" not in b
    assert "IGNORE every" not in b


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


# ---------------------------------------------------------------------------
# CLI-mode inert-protocol strip (the briefing-diet lever).
#
# In cli transport the one-shot worker has no bridge wire, no live peers, and
# no heartbeat — so the inert TM-001..005 / Heartbeat / Agent-Rights /
# # CHANNELS / context-budget-duplicate content is stripped from the in-memory
# briefing (the on-disk SKILL.md + role.j2 stay byte-identical). The carveouts
# (TM-006/007/008, the REPLY CONTRACT, the abandon grammar, the loom-on
# subsection) MUST survive.
# ---------------------------------------------------------------------------


def test_cli_strip_keeps_carveout_anchors():
    """PRESENCE — the cli strip must NOT touch the load-bearing carveouts: the
    surviving hard rules, the REPLY CONTRACT block, and the abandon grammar."""
    b = _cli_briefing()
    for kept in ("TM-006", "TM-007", "TM-008", "# REPLY CONTRACT"):
        assert kept in b, f"carveout anchor dropped by the cli strip: {kept!r}"
    assert "^ABANDON: (?P<category>" in b  # role.j2 abandon grammar survives


def test_cli_strip_removes_pragma_sentence_keeps_schema_version_and_tm007():
    """Cycle-3 — the cli strip removes ONLY role.j2's IDENTITY PRAGMA-assertion
    sentence (its bridge-DB `user_version == <N>` session-open assertion — inert
    for a sessionless one-shot worker, and a duplicate of the rules-block TM-007).
    It KEEPS (a) the preceding `schema_version: <N>` sentence (the `stale_rules`
    abandon hook needs the worker to know its schema_version), and (b) TM-007,
    which survives via the always-prepended team-mode-rules block (`**TM-007 —
    Schema pin.`), NOT via role.j2's now-removed `(TM-007)` reference."""
    b = _cli_briefing()
    # ABSENCE — role.j2's PRAGMA-assertion sentence is gone, anchored on its
    # UNIQUE `== <N>` opener and `(TM-007)` closer. The bare rules-block
    # "asserts `PRAGMA user_version`" statement (NO `==`) is a SEPARATE, canonical
    # TM-007 copy and MUST survive — so we do NOT assert its absence.
    assert "PRAGMA user_version ==" not in b
    assert "hard fail (TM-007)" not in b
    # PRESENCE (a) — the schema_version sentence (stale_rules abandon hook) survives.
    assert "You operate under team-mode-rules `schema_version" in b
    # PRESENCE (b) — TM-007 survives via the rules-block `**TM-007 — Schema pin.`
    # section, not role.j2:19's stripped reference.
    assert "**TM-007 — Schema pin." in b
    # SAFE-DEGRADE — the on-disk role.j2 is untouched (the strip is in-memory and
    # cli-gated), so a re-introduced bridge transport still renders the sentence.
    raw = _ROLE_J2.read_text()
    assert "asserts `PRAGMA user_version == {{ schema_version }}`" in raw
    assert "hard fail (TM-007)" in raw


def test_cli_agent_rights_note_is_channel_agnostic_and_drops_bridge_messages():
    """F4 (cycle 2) — the Agent-Rights section is REPLACED by a channel-agnostic
    auditability nudge: the substance (the worker's work is auditable) survives,
    while the inert `bridge_messages` / bridge-message specifics (a one-shot cli
    worker sends ZERO bridge messages) are GONE from the briefing."""
    b = _cli_briefing()
    # The auditability substance survives, channel-agnostic.
    assert "## Agent Rights" in b
    assert "Your output and full transcript are auditable" in b
    # The inert bridge-message wording the worker can never act on is gone.
    assert "bridge_messages" not in b
    assert "Every bridge message you send or receive" not in b


def test_cli_transport_rule_keeps_loadbearing_clauses_and_drops_restating_prose():
    """F7 (cycle 2) — the trimmed `_CLI_TRANSPORT_RULE` keeps the transport-
    correctness clauses VERBATIM (the structured-final-message contract + the
    UNCHANGED closure-tokens/abandon-grammar/artifacts clause) and drops the
    restating prose (no-peers / host-reads-it-directly)."""
    b = _cli_briefing()
    # KEEP — verbatim load-bearing clauses.
    assert "# TRANSPORT OVERRIDE — CLI MODE" in b
    assert "RETURN YOUR RESULT as the structured final message matching the provided" in b
    assert "that structured output IS your reply to the team-lead" in b
    assert "The closure tokens, the abandon grammar, and the artifacts contract are UNCHANGED" in b
    assert "Emit it exactly once" in b
    # DROP — the restating prose folded out of the trimmed rule.
    assert "no live peers and no inter-agent wire" not in b
    assert "you do not send it anywhere" not in b


def test_cli_strip_removes_inert_protocol_and_clears_5000ch_floor(monkeypatch):
    """ABSENCE + FLOOR — the cli strip removes the inert TM-001..005 / Heartbeat
    / # CHANNELS / context-budget-duplicate content, and removes >= 5000 chars vs
    the unstripped compose (deterministic; fails if any strip gross-regresses)."""
    b = _cli_briefing()
    # Per-strip absence guards — each fails if its individual strip no-ops. The
    # `## Hard rules (TM-001 through TM-008)` heading survives (it frames the
    # kept TM-006..008), so we assert on the bold RULE markers, not bare ids.
    assert "**TM-001 —" not in b
    assert "**TM-005 —" not in b
    assert "## Heartbeat clause" not in b
    assert "30 seconds" not in b  # heartbeat cadence line
    assert "# CHANNELS" not in b
    assert "## Context-budget discipline" not in b
    # Aggregate floor: disable the cli strips → the unstripped "full" briefing,
    # and assert the strip removed >= 5000 chars. Deterministic; ~6.2k headroom.
    monkeypatch.setattr(dispatch, "_strip_cli_inert_rules", lambda t: t)
    monkeypatch.setattr(dispatch, "_strip_context_budget_subsection", lambda t: t)
    monkeypatch.setattr(dispatch, "_strip_cli_channels", lambda r: r)
    full = _cli_briefing()
    assert len(full) - len(b) >= 5000, (
        f"cli strip removed only {len(full) - len(b)} chars (< 5000 floor)"
    )


def test_compose_does_not_mutate_on_disk_rules_or_template():
    """BYTE-PARITY — composing a cli briefing leaves the on-disk SKILL.md and
    role.j2 byte-identical (the strip is in-memory only). pm_dispatch_envelope's
    ABANDON_RE is derived from the on-disk SKILL.md, so any mutation is a bug."""
    before_rules = hashlib.sha256(RULES_SKILL.read_bytes()).hexdigest()
    before_tmpl = hashlib.sha256(_ROLE_J2.read_bytes()).hexdigest()
    _cli_briefing()
    _cli_briefing(team_chat=_loom_ctx())
    assert hashlib.sha256(RULES_SKILL.read_bytes()).hexdigest() == before_rules
    assert hashlib.sha256(_ROLE_J2.read_bytes()).hexdigest() == before_tmpl


def test_cli_strip_keeps_loom_subsection_when_loom_active():
    """LOOM-ON CARVEOUT — a loom-active cli compose still renders the template's
    `## Loom team-chat` subsection (register/deregister/channel) AND keeps the
    rules-block `## Loom chat transport` section, while the # CHANNELS bridge
    table is stripped and the now-dangling `above` back-reference is cleaned up."""
    b = _cli_briefing(team_chat=_loom_ctx())
    # Template loom subsection survives.
    assert "## Loom team-chat" in b
    assert "register" in b
    assert "deregister" in b
    assert "cycle-x" in b  # the loom channel
    # Rules-block loom section survives (footprint loom_section_present_on).
    assert "## Loom chat transport" in b
    # ...but the bridge CHANNELS table is stripped, and the dangling ref is gone.
    assert "# CHANNELS" not in b
    assert "bridge commands above" not in b
