"""Anti-revert presence/contract tests for the settings-recommendation consent
procedure (AI-3) and its references from every user-facing entry skill (AI-4),
plus the CLAUDE.md governance note (AI-5).

These do NOT assert free-form prose — they pin the load-bearing substrings so a
silent revert (deleting the procedure, flipping the default to yes, dropping the
managed-settings safety note, removing the apply/record wiring, or dropping a
skill's pointer) goes RED.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SKILL = REPO / "internal" / "settings-recommendation" / "SKILL.md"

# The five session-lifecycle entry skills that must reference the procedure.
ENTRY_SKILLS = ["run", "load", "save", "ingest", "migrate"]


def test_settings_recommendation_skill_exists():
    assert SKILL.is_file(), f"missing consent procedure: {SKILL}"


def test_settings_recommendation_skill_contract():
    """The procedure wires apply + record, defaults to NO, and carries the
    managed-settings safety caveat."""
    text = SKILL.read_text(encoding="utf-8")
    # The explicit apply call (the only settings writer).
    assert "apply_recommended" in text, "consent procedure lost the apply_recommended call"
    # The version-record call (once-per-version / no-nagging).
    assert "write_state" in text, "consent procedure lost the write_state record call"
    # Default-NO marker (a flip to [Y/n] or [y/Y] would drop this exact token).
    assert "[y/N]" in text, "consent procedure lost the default-NO [y/N] marker"
    # Managed-settings safety note must survive.
    assert "managed-settings.json" in text, "consent procedure dropped the managed-settings caveat"


@pytest.mark.parametrize("skill_name", ENTRY_SKILLS)
def test_entry_skill_references_procedure(skill_name):
    """Each entry skill points at the single procedure file AND names the
    settings_rec_offer trigger token. A dropped pointer leaves that command path
    silently un-offered (the dead-in-prod gap) → RED.

    Parametrized so adding/removing a skill from ENTRY_SKILLS is caught."""
    path = REPO / "skills" / skill_name / "SKILL.md"
    assert path.is_file(), f"entry skill missing: {path}"
    text = path.read_text(encoding="utf-8")
    assert "internal/settings-recommendation/SKILL.md" in text, (
        f"{skill_name} dropped the consent-procedure pointer"
    )
    assert "settings_rec_offer" in text, (
        f"{skill_name} dropped the settings_rec_offer trigger token"
    )


# ── AI-5: CLAUDE.md governance note ────────────────────────────────────────────


def test_claude_md_documents_settings_recommendation():
    """CLAUDE.md '## Model recommendations' documents the opt-in lever: the
    recommended triple, the consent/opt-in + once-per-version semantics."""
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## Model recommendations" in text
    # The recommended triple.
    assert "sonnet" in text
    assert "effortLevel" in text
    assert "autoCompactEnabled" in text
    # Governance words for this lever (consent/opt-in + once-per-version).
    lowered = text.lower()
    assert "consent" in lowered or "opt-in" in lowered
    assert "once per version" in lowered or "once-per-version" in lowered


def test_claude_md_documents_advisory_enforcement_model():
    """CLAUDE.md states the ENFORCEMENT MODEL for this lever: the y/N
    presentation is ADVISORY (agent reads the procedure markdown), while the
    SAFETY (read-only compute, single explicit writer, merge-safe/atomic,
    managed-settings never touched) is code-enforced.

    ANTI-REVERT (reviewer finding — agent-systems-architect): pins the honest
    advisory-vs-code-enforced distinction so a silent deletion of the
    enforcement-model note (which would re-open the 'is the offer guaranteed to
    surface?' ambiguity the reviewer flagged) goes RED."""
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    # Scope the assertions to the lever's own subsection so generic
    # 'advisory'/skill-name hits elsewhere in CLAUDE.md cannot mask a revert.
    anchor = "### First-session settings recommendation on version upgrade"
    assert anchor in text, "the settings-recommendation subsection header was removed"
    section = text[text.index(anchor) :]
    # Stop at the next top-level subsection so we only inspect this lever's prose.
    nxt = section.find("\n### ", len(anchor))
    section = section[:nxt] if nxt != -1 else section
    sect_lower = section.lower()
    # The presentation is named as advisory (not silently implied as enforced).
    assert "advisory" in sect_lower, "enforcement-model note (advisory presentation) was dropped"
    # The safety distinction: code-enforced read-only/consent, not advisory.
    assert "code-enforced" in sect_lower or "code path" in sect_lower, (
        "enforcement-model note lost the code-enforced-safety distinction"
    )
    # The mitigation: the note credits the all-five-entry-skills pointer coverage.
    assert "five entry skills" in sect_lower or "all five" in sect_lower, (
        "enforcement-model note lost the all-entry-skills gap-minimization point"
    )
