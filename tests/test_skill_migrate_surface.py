"""The /atelier:migrate skill is the 5th user-facing surface (v1.1.0).
This test pins its presence + frontmatter so accidental removal fails CI."""

import re
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_migrate_skill_file_exists():
    assert (REPO / "skills" / "migrate" / "SKILL.md").is_file()


def test_migrate_skill_has_description_frontmatter():
    """Per CLAUDE.md, every public skill at skills/<name>/SKILL.md
    must carry YAML frontmatter with a `description` field."""
    md = (REPO / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", md, re.DOTALL)
    assert m is not None, "skills/migrate/SKILL.md missing YAML frontmatter"
    fm = m.group(1)
    assert "description:" in fm, "frontmatter missing description field"
    # Description routing trigger should mention 'migrate' so /atelier:migrate
    # autocompletes for users typing 'migr…'
    assert "migrat" in fm.lower()


def test_migrate_skill_routes_to_internal_procedure():
    """The skill body should reference the internal procedure that does
    the actual work — same routing pattern as the other 4 skills."""
    md = (REPO / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    assert "internal/migrate-local-to-memex/SKILL.md" in md
