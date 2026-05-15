"""Validates skills/using-atelier/SKILL.md is parseable and complete."""
import re
from pathlib import Path

import yaml

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "using-atelier" / "SKILL.md"


def _read_skill():
    text = SKILL_PATH.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert m is not None, "SKILL.md missing YAML frontmatter delimited by ---"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_skill_file_exists():
    """SKILL.md must exist at the canonical path."""
    assert SKILL_PATH.exists(), f"{SKILL_PATH} does not exist"


def test_frontmatter_has_required_keys():
    """Frontmatter must define name=using-atelier and a Use-when description."""
    frontmatter, _ = _read_skill()
    assert "name" in frontmatter
    assert frontmatter["name"] == "using-atelier"
    assert "description" in frontmatter
    assert "Use when" in frontmatter["description"]


def test_body_has_required_sections():
    """All five canonical sections must be present."""
    _, body = _read_skill()
    required_sections = [
        "## Trigger contract",
        "## Red Flags",
        "## Phase guidance",
        "## Dev arc",
        "## Bypass procedure",
    ]
    for section in required_sections:
        assert section in body, f"missing section: {section}"


def test_phase_guidance_table_has_all_phases():
    """Every non-terminal phase from migration 003 must appear in the phase guidance table."""
    _, body = _read_skill()
    phase_section_match = re.search(
        r"## Phase guidance\n(.*?)(?=\n## )", body, re.DOTALL,
    )
    assert phase_section_match, "Phase guidance section not found or improperly closed"
    phase_block = phase_section_match.group(1)

    expected_phases = {
        "design:open", "design:approved",
        "plan:open", "plan:approved",
        "tdd:red", "tdd:green", "tdd:clean",
        "review:open", "review:changes-requested", "review:approved",
        "security:open", "security:approved",
        "qa:open", "qa:approved",
        "diagnose:open", "diagnose:resolved",
        "handoff:complete",
    }
    for phase in expected_phases:
        assert f"`{phase}`" in phase_block, f"phase '{phase}' missing from phase guidance"


def test_dev_arc_references_canonical_flow():
    """Dev arc section must mention every phase in canonical order."""
    _, body = _read_skill()
    arc_section_match = re.search(r"## Dev arc\n(.*?)(?=\n## )", body, re.DOTALL)
    assert arc_section_match, "Dev arc section not found or improperly closed"
    arc = arc_section_match.group(1)
    for phase in ["design", "plan", "tdd", "review", "security", "qa", "handoff"]:
        assert phase in arc, f"dev arc missing '{phase}'"


def test_trigger_contract_describes_three_routings():
    """Trigger contract must define three routings: full arc, diagnose, direct."""
    _, body = _read_skill()
    contract = re.search(r"## Trigger contract\n(.*?)(?=\n## )", body, re.DOTALL).group(1)
    assert "Full Atelier arc" in contract or "full arc" in contract.lower()
    assert "diagnose" in contract.lower()
    assert "directly" in contract.lower() or "without" in contract.lower()


def test_red_flags_table_present():
    """Red Flags table must have at least 5 substantive rows (from spec §2.3)."""
    _, body = _read_skill()
    red_flags = re.search(r"## Red Flags\n(.*?)(?=\n## )", body, re.DOTALL).group(1)
    table_rows = [r for r in red_flags.split("\n") if r.strip().startswith("|") and "---" not in r]
    assert len(table_rows) >= 6, f"Red Flags table needs more rows; found {len(table_rows)}"
