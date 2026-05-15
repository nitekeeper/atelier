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


@pytest.mark.parametrize("skill", UNWALLED + WALLED)
def test_step_1_check_gate_uses_db_path_arg(skill):
    """The check-gate invocation must include <db_path> as positional arg."""
    path = SKILLS_DIR / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # Find the check-gate line(s) and verify <db_path> appears before check-gate
    import re
    matches = re.findall(r"workflow\.py\s+(\S+)\s+check-gate", text)
    assert matches, f"{skill}: no check-gate invocation found"
    for arg in matches:
        assert arg == "<db_path>" or arg.startswith("<"), (
            f"{skill}: check-gate first positional arg should be <db_path>, got '{arg}'"
        )


def test_diagnose_step_1_records_pre_diagnose_phase():
    """dev-diagnose step 1 must document that current_phase is recorded as pre_diagnose_phase."""
    text = (SKILLS_DIR / "dev-diagnose" / "SKILL.md").read_text(encoding="utf-8")
    assert "pre_diagnose_phase" in text, (
        "dev-diagnose: step 1 must record current_phase as <pre_diagnose_phase> for restoration"
    )
