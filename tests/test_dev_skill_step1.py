"""Verify dev skills' step 1 follows the GateResult-aware pattern."""
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

UNWALLED = ["dev-design", "dev-diagnose", "dev-handoff"]
WALLED = ["dev-plan", "dev-tdd", "dev-review", "dev-security", "dev-qa"]


@pytest.mark.parametrize("skill", UNWALLED + WALLED)
def test_step_1_uses_json_check_gate(skill):
    """All dev skills' step 1 invokes check-gate and parses JSON output."""
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "check-gate" in text, f"{skill}: step 1 must invoke check-gate"
    assert "allowed" in text, f"{skill}: must reference the JSON allowed field"


@pytest.mark.parametrize("skill", UNWALLED)
def test_unwalled_skill_does_not_describe_bypass_branch(skill):
    """Unwalled skills don't need the bypass prompt - their allowed is always true."""
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "Proceed anyway" not in text, (
        f"{skill}: should not describe bypass prompt (allowed is always true)"
    )


@pytest.mark.parametrize("skill", WALLED)
def test_walled_skill_implements_bypass_flow(skill):
    """Walled skills' step 1 must include the user-confirm-and-log bypass flow."""
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "Proceed anyway" in text, f"{skill}: must include the bypass prompt"
    assert "log-bypass" in text, f"{skill}: must call log-bypass on confirmed bypass"
    assert "advance" in text.lower(), f"{skill}: must tell user how to advance phase on bypass=no"
