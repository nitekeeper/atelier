"""Verify session-lifecycle skills have YAML frontmatter with required keys."""
import re
from pathlib import Path

import yaml
import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@pytest.mark.parametrize("skill_name", ["ingest", "save", "load"])
def test_session_skill_has_frontmatter(skill_name):
    """Each session-lifecycle skill must declare YAML frontmatter with name + description."""
    path = SKILLS_DIR / skill_name / "SKILL.md"
    assert path.exists(), f"{path} does not exist"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert m, f"{skill_name}: SKILL.md missing YAML frontmatter delimited by ---"
    data = yaml.safe_load(m.group(1))
    assert data.get("name") == skill_name, f"{skill_name}: frontmatter name mismatch"
    assert "description" in data, f"{skill_name}: description key missing"
    assert "Use when" in data["description"], (
        f"{skill_name}: description must start with 'Use when…' trigger phrasing"
    )
