"""Verify dev-handoff retro queries phase_bypasses and surfaces patterns."""
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def test_handoff_skill_references_phase_bypasses_query():
    """dev-handoff must query phase_bypasses to surface bypasses in retro."""
    path = SKILLS_DIR / "dev-handoff" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "phase_bypasses" in text, (
        "dev-handoff must query phase_bypasses to surface bypasses in retro"
    )
    # Must aggregate by skill so retro is readable
    assert "GROUP BY" in text or "aggregat" in text.lower() or "by skill" in text.lower()


def test_handoff_describes_bypass_section_in_retro():
    """dev-handoff retro must include a Bypasses section."""
    path = SKILLS_DIR / "dev-handoff" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "Bypass" in text or "bypass" in text


def test_handoff_handles_no_bypasses_gracefully():
    """The retro must describe the empty-bypasses case."""
    path = SKILLS_DIR / "dev-handoff" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # Look for a phrase indicating empty handling
    assert (
        "No phase bypasses" in text
        or "no bypasses" in text.lower()
        or "0 bypass" in text
    ), "dev-handoff must describe what to write when there are no bypasses"
