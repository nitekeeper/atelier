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

from scripts.dispatch import (
    ROLE_TEMPLATE,
    _strip_cli_role_inert,
    compose_briefing,
    make_template_env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _real_persona() -> str:
    persona_json = json.loads(
        (REPO_ROOT / "templates" / "agents" / "backend-engineer-1.json").read_text(encoding="utf-8")
    )
    return persona_json["profile"]


def _real_phase() -> str:
    return (REPO_ROOT / "internal" / "dev-tdd" / "SKILL.md").read_text(encoding="utf-8")


def _compose_cli(
    *,
    peers: list | None,
    forbidden_actions: list | None,
    acceptance_criteria: list | None,
) -> str:
    """Compose a real backend-engineer-1 briefing in the default (cli) transport.

    No ``transport=`` is passed, so ``resolve_transport()`` resolves the only valid
    transport — ``cli`` — exactly as the production host caller does."""
    return compose_briefing(
        role_id="backend-engineer-1",
        task_id=7,
        persona_profile_text=_real_persona(),
        phase_procedure_text=_real_phase(),
        task_brief="Add a unit test for X.",
        team_id="atelier-diet-team-1",
        team_lead_name="team-lead",
        wave_id="wave-3",
        wave_phase="tdd:green",
        deadline_iso="2099-01-01T00:00:00Z",
        peers=peers,
        forbidden_actions=forbidden_actions,
        acceptance_criteria=acceptance_criteria,
    )


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

    # In cli mode the inert TM-001..005 RULE bodies are stripped (the one-shot
    # worker has no bridge wire / peers / heartbeat); the carveout TM-006 survives
    # and proves the rules block was still prepended. (The "## Hard rules (TM-001
    # through TM-008)" heading survives too, so we assert on the bold RULE marker.)
    assert "**TM-001 —" not in rendered
    assert "TM-006" in rendered

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
        "# WAVE CONTEXT",
        "# TASK",
        "# ABANDON GRAMMAR",
    ):
        assert anchor in rendered, f"rendered briefing missing structural anchor: {anchor!r}"
    # The bridge # CHANNELS block is inert for a one-shot cli worker — stripped.
    assert "# CHANNELS" not in rendered


# ---------------------------------------------------------------------------
# CLI briefing diet — cycle-1 levers (role.j2 strips)
#
# Lever 1: the empty Peers / Forbidden / Acceptance-criteria subsections are
#          template-guarded (`{% if peers %}` etc.) — they do NOT render when the
#          list is empty (every production cli spawn), but DO render for a future
#          caller that populates them (safe-degrade).
# Lever 2: the reply-contract "Shutdown handshake (TM-005)…" paragraph + its
#          shutdown_response json example is stripped post-render for cli.
# Lever 3: the IDENTITY "Your own agent handle on the bridge is `<self>`." line is
#          stripped post-render for cli.
# Levers 2/3 are TRANSPORT-gated (cli), independent of the peers/forbidden lists.
# ---------------------------------------------------------------------------

# Surfaces stripped on a production (empty-list) cli spawn.
_STRIPPED_ON_EMPTY_CLI = (
    "Shutdown handshake (TM-005)",  # lever 2 — handshake prose
    "shutdown_response",  # lever 2 — handshake json example
    "shutdown_request",  # lever 2 — handshake trigger mention
    "Your own agent handle on the bridge is",  # lever 3 — bridge-handle line
    "## Peers in this wave",  # lever 1 — empty Peers subsection
    "## Forbidden in this wave",  # lever 1 — empty Forbidden subsection
    "## Acceptance criteria",  # lever 1 — empty Acceptance subsection
)

# Load-bearing surfaces that MUST survive every cli render (the strips must never
# reach these). TM-006 closure tokens, the task_result envelope field names, the
# always-on WAVE CONTEXT table, and the IDENTITY schema_version / PRAGMA paragraph
# (the `stale_rules` abandon category depends on the schema_version mention).
_MUST_SURVIVE_CLI = (
    "# WAVE CONTEXT",
    "| Wave ID",
    "| Quorum rule",
    "`done`",
    "`blocked`",
    "`needs-input`",
    "`abandoned`",
    "`failed`",
    '"type": "task_result"',
    '"task_id"',
    '"attempt"',
    '"status"',
    '"artifacts"',
    '"notes_md"',
    '"next_action"',
    "schema_version",
    "PRAGMA user_version",
)


def test_cli_briefing_strips_inert_role_surfaces_when_empty() -> None:
    """Production path: a cli spawn with empty peers/forbidden/acceptance (what
    `_host_briefing_for` passes) drops every inert role.j2 surface AND keeps every
    load-bearing surface. Also asserts the strip leaves no triple-newline seam."""
    rendered = _compose_cli(peers=None, forbidden_actions=None, acceptance_criteria=None)

    for surface in _STRIPPED_ON_EMPTY_CLI:
        assert surface not in rendered, f"inert surface survived the cli strip: {surface!r}"
    for surface in _MUST_SURVIVE_CLI:
        assert surface in rendered, f"load-bearing surface lost in cli render: {surface!r}"
    # The WAVE CONTEXT table is always-on even though its Peers/Forbidden
    # subsections were guarded away.
    assert "# WAVE CONTEXT" in rendered and "| Phase" in rendered
    # Seam cleanliness — the post-render strips must not leave a triple newline.
    assert "\n\n\n" not in rendered


def test_cli_briefing_keeps_subsections_when_populated() -> None:
    """Lever-1 safe-degrade: a future caller that POPULATES peers/forbidden/
    acceptance still renders those subsections (the `{% if %}` guard fires on
    truthiness, not transport) — even in cli. The transport-gated levers 2/3
    (handshake + bridge-handle) are STILL stripped regardless of the lists."""
    rendered = _compose_cli(
        peers=[{"role_id": "sdet-1", "mandate": "Author concurrency tests."}],
        forbidden_actions=["Touching paths outside the atelier clone."],
        acceptance_criteria=["pytest -q is green on tests/."],
    )
    # Lever-1 subsections render when populated.
    assert "## Peers in this wave" in rendered
    assert "sdet-1" in rendered and "Author concurrency tests." in rendered
    assert "## Forbidden in this wave" in rendered
    assert "Touching paths outside the atelier clone." in rendered
    assert "## Acceptance criteria" in rendered
    assert "pytest -q is green on tests/." in rendered
    # Levers 2/3 are transport-gated, so still stripped even with populated lists.
    assert "Shutdown handshake (TM-005)" not in rendered
    assert "Your own agent handle on the bridge is" not in rendered


def test_cli_briefing_mixed_guards_render_independently() -> None:
    """Lever-1 guards fire PER-SUBSECTION, not all-or-nothing: a mixed case with
    `peers` populated but `forbidden_actions` / `acceptance_criteria` empty renders
    ONLY the Peers subsection (+ its bullet); the two empty subsections are guarded
    away. Proves each `{% if %}` is independent and leaves a clean (no triple-
    newline) seam where the empty ones dropped out."""
    rendered = _compose_cli(
        peers=[{"role_id": "sdet-1", "mandate": "Author concurrency tests."}],
        forbidden_actions=[],
        acceptance_criteria=[],
    )
    # Populated subsection renders, with its bullet.
    assert "## Peers in this wave" in rendered
    assert "sdet-1" in rendered and "Author concurrency tests." in rendered
    # Empty subsections are guarded away.
    assert "## Forbidden in this wave" not in rendered
    assert "## Acceptance criteria" not in rendered
    # The always-on WAVE CONTEXT table still stands.
    assert "# WAVE CONTEXT" in rendered and "| Quorum rule" in rendered
    # Clean seam where the empty subsections dropped out.
    assert "\n\n\n" not in rendered


def test_raw_role_template_keeps_handshake_and_handle_pre_strip() -> None:
    """Levers 2/3 safe-degrade: the on-disk role.j2 is byte-unchanged — the
    handshake + bridge-handle live in the RENDERED template and are removed ONLY
    by the cli post-render strip. Rendering the template directly (the form any
    non-cli / re-introduced bridge transport would yield, since the strip runs
    only under `transport == TRANSPORT_CLI`) proves the surfaces are still there
    pre-strip, so a future bridge transport safe-degrades to the full briefing."""
    from tests.test_dispatch_templates import _full_context

    raw = make_template_env().get_template(ROLE_TEMPLATE).render(**_full_context())
    assert "Shutdown handshake (TM-005)" in raw
    assert "shutdown_response" in raw
    assert "Your own agent handle on the bridge is" in raw
    # Populated context → the lever-1 subsections render here too.
    assert "## Peers in this wave" in raw
    assert "## Forbidden in this wave" in raw
    assert "## Acceptance criteria" in raw


def test_strip_cli_role_inert_unit() -> None:
    """`_strip_cli_role_inert` removes both inert surfaces, preserves the
    load-bearing task_result envelope / TM-006 surface between them, stops at the
    FIRST closing fence (non-greedy — a later unrelated fenced block survives), and
    is a safe no-op when neither anchor is present."""
    sample = (
        "# IDENTITY\n\n"
        "DB at session open; mismatch is a hard fail (TM-007).\n\n"
        "Your own agent handle on the bridge is `backend-engineer-1`.\n"
        "# REPLY CONTRACT\n\n"
        '"type": "task_result" — keep me\n'
        "| `done` | `blocked` | `failed` | keep this TM-006 table too |\n\n"
        "Shutdown handshake (TM-005). On receiving `{...}`, reply with:\n\n"
        '```json\n{"type":"shutdown_response", "approve": true}\n```\n'
        "# TASK\n\n"
        # A SECOND, unrelated downstream closing ``` fence. If the shutdown regex
        # were greedy it would run past the handshake's own fence to THIS one and
        # delete everything between — including `# TASK` and the block below. The
        # non-greedy `.*?` must stop at the FIRST fence, leaving this untouched.
        "Here is an unrelated downstream fenced block that MUST survive:\n\n"
        "```python\nprint('downstream survives')\n```\n"
    )
    out = _strip_cli_role_inert(sample)
    # Both inert surfaces gone.
    assert "Your own agent handle on the bridge is" not in out
    assert "Shutdown handshake (TM-005)" not in out
    assert "shutdown_response" not in out
    # Load-bearing surfaces between/around them survive.
    assert '"type": "task_result" — keep me' in out
    assert "keep this TM-006 table too" in out
    assert "mismatch is a hard fail (TM-007)." in out
    # First-fence-stop proof: `# TASK` and the unrelated downstream fenced block
    # AFTER the handshake survive intact (the regex did not run past the first fence).
    assert "# TASK" in out
    assert "Here is an unrelated downstream fenced block that MUST survive:" in out
    assert "```python\nprint('downstream survives')\n```" in out
    # No collapsed-into-jumble: the surrounding headings still stand on their own.
    assert "# REPLY CONTRACT" in out
    assert "\n\n\n" not in out

    # Safe no-op when neither anchor is present.
    benign = "# SOMETHING\n\nplain text with no inert surfaces here.\n"
    assert _strip_cli_role_inert(benign) == benign
