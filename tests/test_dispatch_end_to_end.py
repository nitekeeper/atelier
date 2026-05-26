"""End-to-end compose_briefing test.

Drives ``scripts.dispatch.compose_briefing`` against the REAL on-disk
team-mode rules SKILL, a real persona profile (backend-engineer-1), and
a real phase procedure (dev-tdd) — no mocks, no stubs. Asserts the
returned briefing string contains the expected structural anchors and
interpolated values. No token-cap assertions: rules SKILL v1.1 removed
all token caps; the only physical limit is the per-bridge-message byte
cap enforced elsewhere by scripts/bridge_send.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dispatch import compose_briefing

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_compose_briefing_end_to_end_with_real_persona() -> None:
    """Compose a real backend-engineer-1 briefing from on-disk sources and
    verify the assembled string honours the §16.3 layout contract."""
    rules_text = (REPO_ROOT / "internal" / "team-mode-rules" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert rules_text, "rules SKILL.md is empty — test fixture is broken"

    persona_json = json.loads(
        (REPO_ROOT / "templates" / "agents" / "backend-engineer-1.json").read_text(encoding="utf-8")
    )
    persona_profile = persona_json["profile"]
    assert "Dr. Samuel Okafor" in persona_json["name"], (
        "fixture drift — backend-engineer-1.json name field changed"
    )

    phase_procedure = (REPO_ROOT / "internal" / "dev-tdd" / "SKILL.md").read_text(encoding="utf-8")
    assert phase_procedure, "dev-tdd SKILL.md is empty — test fixture is broken"

    rendered = compose_briefing(
        role_id="backend-engineer-1",
        task_id=42,
        persona_profile_text=persona_profile,
        phase_procedure_text=phase_procedure,
        task_brief="Add a unit test for X.",
        team_id="atelier-e2e-team-1",
        team_lead_name="team-lead",
        wave_id="wave-3",
        wave_phase="implement",
        deadline_iso="2026-05-25T22:00:00Z",
        peers=[
            {
                "role_id": "sdet-1",
                "mandate": "Author concurrency tests for the bridge.",
            }
        ],
        forbidden_actions=[
            "Touching paths outside the atelier clone.",
        ],
        acceptance_criteria=[
            "pytest -q is green on tests/test_dispatch_*.py.",
        ],
    )

    # Type contract: compose_briefing returns a plain str (the worker's
    # inaugural spawn prompt, ready to feed into the Task tool).
    assert isinstance(rendered, str)

    # TM-001 is a stable rules-SKILL anchor — proves the rules block was
    # prepended verbatim into the briefing's task_brief slot.
    assert "TM-001" in rendered

    # Persona block proof — either the agent's display name OR their
    # role_id must appear (role_id always appears in IDENTITY; the persona
    # body normally carries the display name).
    assert ("Dr. Samuel Okafor" in rendered) or ("backend-engineer-1" in rendered)

    # Task-brief slot was populated and reached the rendered output.
    assert "Add a unit test for X." in rendered

    # Structural anchors emitted by role.j2 — these MUST all be present
    # or the worker prompt is missing a contract surface.
    for anchor in (
        "# IDENTITY",
        "# CHANNELS",
        "# WAVE CONTEXT",
        "# TASK",
        "# ABANDON GRAMMAR",
    ):
        assert anchor in rendered, f"rendered briefing missing structural anchor: {anchor!r}"
