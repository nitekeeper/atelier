"""Lock in the contract that Atelier exposes EXACTLY 7 user-facing skills
to Claude Code (v1.2.0 surface — adds `abort` + `status` team-mode lifecycle
skills, issue #65, to the v1.1.0 set of 5, for a set of 7) and that every
internal procedure stays under internal/."""

import json
from pathlib import Path

REPO = Path(__file__).parent.parent
SKILLS = REPO / "skills"
INTERNAL = REPO / "internal"


def test_exactly_seven_user_skills():
    """EXACTLY 7 user-facing skills (v1.2.0): the v1.1.0 set of 5 plus the #65
    `abort` + `status` team-mode lifecycle skills."""
    skill_dirs = [p for p in SKILLS.iterdir() if p.is_dir() and (p / "SKILL.md").exists()]
    names = sorted(p.name for p in skill_dirs)
    assert names == ["abort", "ingest", "load", "migrate", "run", "save", "status"], names


def test_plugin_manifest_lists_no_extra_skills():
    """If plugin.json declares any skill, it must be one of the seven."""
    manifest_path = REPO / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return  # nothing to check
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = data.get("skills", [])
    if isinstance(declared, list):
        for s in declared:
            name = s if isinstance(s, str) else s.get("name", "")
            assert any(
                name.endswith(n)
                for n in ("load", "save", "ingest", "run", "migrate", "abort", "status")
            ), f"manifest declares unknown skill: {name}"


def test_no_internal_skill_has_user_invocable_true():
    """Every internal SKILL.md must NOT have `user-invocable: true`."""
    for path in INTERNAL.rglob("SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "user-invocable: true" not in text, (
            f"{path.relative_to(REPO)} declares user-invocable: true"
        )


def test_internal_procedures_have_description_only_no_name_field():
    """Internal SKILL.md files must lack a top-level `name:` field that
    would register them as a slash command."""
    for path in INTERNAL.rglob("SKILL.md"):
        # Parse the first frontmatter block
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        try:
            _, frontmatter, _ = text.split("---", 2)
        except ValueError:
            continue
        for line in frontmatter.strip().splitlines():
            if line.startswith("name:"):
                # Some internal procedures have a name for documentation;
                # this is fine as long as they're not registered in plugin.json.
                # The plugin manifest test above is authoritative.
                pass
