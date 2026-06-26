"""Regression tests for Loom-availability gating of the rules-block Loom section.

The "## Loom chat transport" section of the team-mode rules block is behavioural
ONLY when Loom is the active chat transport. `compose_briefing` now strips it
from the injected briefing when Loom is NOT active (team_chat None / bridge), and
keeps it verbatim when Loom is live — so the ~3.2KB section is not paid on every
spawn in the common (no-Loom) case, while the F16/A9 mandatory-when-available
contract is preserved when Loom is up.
"""

from pathlib import Path

from scripts.dispatch import compose_briefing
from scripts.loom_comms import LoomStatus, build_team_chat_context

_KW = {
    "role_id": "backend-engineer-1",
    "task_id": "t-1",
    "persona_profile_text": "(persona)",
    "phase_procedure_text": "(phase)",
    "task_brief": "(task)",
    "team_id": "team-1",
    "team_lead_name": "PM",
    "wave_id": "w-1",
    "wave_phase": "tdd:green",
    "deadline_iso": "2099-12-31T23:59:59+00:00",
    "transport": "cli",
}

# A real loom-transport ctx (the shape role.j2's CHANNELS subsection renders).
_LOOM_CTX = build_team_chat_context(
    LoomStatus(available=True, client=Path("/fake/loom_chat.py")),
    role_id="backend-engineer-1",
    channel="atelier-bench",
    team_lead_name="PM",
)


def test_loom_section_stripped_when_loom_inactive():
    # team_chat=None → no live Loom → section stripped.
    off = compose_briefing(**_KW, team_chat=None)
    assert "## Loom chat transport" not in off
    # bridge transport → also no live Loom → stripped.
    bridge = compose_briefing(**_KW, team_chat={"transport": "bridge"})
    assert "## Loom chat transport" not in bridge


def test_loom_section_kept_when_loom_active():
    on = compose_briefing(**_KW, team_chat=_LOOM_CTX)
    # The mandatory-when-available Loom contract MUST survive verbatim.
    assert "## Loom chat transport" in on
    assert "ATELIER_LOOM_COMMS=0" in on
    assert "ALWAYS" in on and "ride the" in on  # bridge-exclusivity clause
    # Pin the rest of the load-bearing Loom contract on the keep-path so a
    # future bad edit to the source section is caught here.
    assert "500" in on  # ≤500-char chat-body cap
    assert "@here" in on  # the one deliberate broadcast carve-out
    assert "rejoin" in on  # deregister/rejoin lifecycle


def test_loom_gating_saves_tokens_when_inactive():
    off = compose_briefing(**_KW, team_chat=None)
    on = compose_briefing(**_KW, team_chat=_LOOM_CTX)
    # loom-off briefing is strictly smaller (the section is gone) by ~3KB.
    assert len(on) - len(off) >= 2500


def test_non_loom_rules_survive_when_inactive():
    # Stripping the Loom section must not touch other load-bearing rules.
    off = compose_briefing(**_KW, team_chat=None)
    for n in range(1, 9):
        assert f"TM-00{n}" in off
    assert "<untrusted source=" in off  # TM-008 fence
    assert "30 seconds" in off  # heartbeat clause (immediately follows Loom)
    assert "Self-verify" in off
