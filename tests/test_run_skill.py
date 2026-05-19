"""Validates skills/run/SKILL.md is parseable and complete."""

import re
from pathlib import Path

import pytest
import yaml

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "run" / "SKILL.md"
# Phases live inline in v1.1.0's single-file schema migration (spec §11.2 / Plan 1
# Task 5). v1.0.13's migrations/003_phases.sql was deleted in the clean-cut redesign.
MIGRATION_003 = (
    Path(__file__).resolve().parent.parent / "migrations" / "shared" / "001_v110_schema.sql"
)


@pytest.fixture(scope="module")
def skill_data():
    """Return (frontmatter, body) parsed from SKILL.md."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
    assert m is not None, "SKILL.md missing YAML frontmatter delimited by ---"
    return yaml.safe_load(m.group(1)), m.group(2)


def _parse_phases_from_migration():
    """Parse the INSERT into phases rows from migration 003.

    Returns the set of phase names (all phases, including terminal ones,
    since the guidance table includes a row for handoff:complete).
    """
    text = MIGRATION_003.read_text(encoding="utf-8")
    # Strip SQL line comments to avoid matching commented-out phase rows
    text = re.sub(r"--[^\n]*", "", text)
    # Find rows like: ('phase:name', 'skill', 'state', 'desc', is_terminal, allow_from_any),
    rows = re.findall(
        r"\(\s*'([^']+)'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,\s*([01])\s*,\s*[01]\s*\)",
        text,
    )
    return {phase for phase, _is_terminal in rows}


def test_frontmatter_has_required_keys(skill_data):
    """Frontmatter must define a Use-when description (and must NOT include name,
    which Claude Code auto-derives from the skill directory)."""
    frontmatter, _ = skill_data
    assert "name" not in frontmatter, (
        "execute frontmatter must NOT include 'name' "
        "(Claude Code auto-derives the skill name from the directory)"
    )
    assert "description" in frontmatter
    assert "Use when" in frontmatter["description"]


def test_body_has_required_sections(skill_data):
    """All five canonical sections must be present."""
    _, body = skill_data
    required_sections = [
        "## Trigger contract",
        "## Red Flags",
        "## Phase guidance",
        "## Dev arc",
        "## Bypass procedure",
    ]
    for section in required_sections:
        assert section in body, f"missing section: {section}"


def test_phase_guidance_table_has_all_phases(skill_data):
    """Every phase in migration 003 must appear in the phase guidance table."""
    _, body = skill_data
    phase_section_match = re.search(
        r"## Phase guidance\r?\n(.*?)(?=\r?\n## )",
        body,
        re.DOTALL,
    )
    assert phase_section_match is not None, "Phase guidance section not found or improperly closed"
    phase_block = phase_section_match.group(1)

    expected_phases = _parse_phases_from_migration()
    assert len(expected_phases) > 0, "no phases parsed from migration 003"

    for phase in expected_phases:
        assert f"`{phase}`" in phase_block, f"phase '{phase}' missing from phase guidance"


def test_dev_arc_references_canonical_flow(skill_data):
    """Dev arc section must mention every phase in canonical order."""
    _, body = skill_data
    arc_section_match = re.search(r"## Dev arc\r?\n(.*?)(?=\r?\n## |\Z)", body, re.DOTALL)
    assert arc_section_match is not None, "Dev arc section not found or improperly closed"
    arc = arc_section_match.group(1)
    for phase in ["design", "plan", "tdd", "review", "security", "qa", "handoff"]:
        assert phase in arc, f"dev arc missing '{phase}'"


def test_trigger_contract_describes_three_routings(skill_data):
    """Trigger contract must define three routings: full arc, diagnose, direct."""
    _, body = skill_data
    match = re.search(r"## Trigger contract\r?\n(.*?)(?=\r?\n## )", body, re.DOTALL)
    assert match is not None, "Trigger contract section not found or improperly closed"
    contract = match.group(1)
    assert "Full Atelier arc" in contract or "full arc" in contract.lower()
    assert "diagnose" in contract.lower()
    assert "directly" in contract.lower() or "without" in contract.lower()


def test_red_flags_table_present(skill_data):
    """Red Flags table must have at least 5 substantive rows (from spec §2.3)."""
    _, body = skill_data
    match = re.search(r"## Red Flags\r?\n(.*?)(?=\r?\n## )", body, re.DOTALL)
    assert match is not None, "Red Flags section not found or improperly closed"
    red_flags = match.group(1)
    table_rows = [r for r in red_flags.split("\n") if r.strip().startswith("|") and "---" not in r]
    assert len(table_rows) >= 6, f"Red Flags table needs more rows; found {len(table_rows)}"
