"""Anti-revert presence/contract tests for the settings-recommendation consent
procedure (AI-3) and its references from every user-facing entry skill (AI-4),
plus the CLAUDE.md governance note (AI-5).

These do NOT assert free-form prose — they pin the load-bearing substrings so a
silent revert (deleting the procedure, flipping the default to yes, dropping the
managed-settings safety note, removing the apply/record wiring, or dropping a
skill's pointer) goes RED.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SKILL = REPO / "internal" / "settings-recommendation" / "SKILL.md"

# The five session-lifecycle entry skills that must reference the procedure.
ENTRY_SKILLS = ["run", "load", "save", "ingest", "migrate"]


def test_settings_recommendation_skill_exists():
    assert SKILL.is_file(), f"missing consent procedure: {SKILL}"


def test_settings_recommendation_skill_contract():
    """The procedure wires the profile apply + record, presents a NAMED-PROFILE
    menu (cost-effective default + code-quality + explicit skip), documents that
    Enter/empty APPLIES the cost-effective default, keeps an explicit
    skip-writes-nothing option, and carries the managed-settings safety caveat."""
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()
    # The explicit profile-apply call (the only settings writer).
    assert "apply_profile" in text, "consent procedure lost the apply_profile call"
    # The version-record call (once-per-version / no-nagging).
    assert "write_state" in text, "consent procedure lost the write_state record call"
    # Both named profiles must be offered.
    assert "cost-effective" in text, "menu lost the cost-effective profile"
    assert "code-quality" in text, "menu lost the code-quality profile"
    # cost-effective is the marked recommended default.
    assert "recommended" in lowered, "menu lost the recommended-default marker"
    # An explicit skip option must still exist (writes nothing).
    assert "skip" in lowered, "menu lost the explicit skip option"
    # The subagent-model env mechanism + the subagent-effort limitation.
    assert "CLAUDE_CODE_SUBAGENT_MODEL" in text, "procedure lost the subagent-model env key"
    assert "effort" in lowered, "procedure lost the subagent-effort limitation note"
    # Managed-settings safety note must survive.
    assert "managed-settings.json" in text, "consent procedure dropped the managed-settings caveat"


def test_skill_enter_applies_cost_effective_default():
    """REVERSED consent default: Enter / an EMPTY answer APPLIES the
    cost-effective profile (informed consent — the menu says Enter
    applies/writes the posture). An explicit skip still writes nothing; an
    unrecognized typo re-asks once (no accidental write).

    ANTI-REVERT (hardened): the positive pin is a CO-LOCATED phrase tying
    `enter` to APPLY/WRITE the `cost-effective` profile (bare substrings like
    "enter"/"write" occur generically and would pass on a reverted doc); the
    negative pin is PHRASING-AGNOSTIC — it forbids `enter`/`empty`/`blank`
    appearing near a skip verb (so a differently-worded revert such as "a blank
    answer skips" is caught, not just the exact old phrase). The re-ask-once
    guard — which a genuine empty=skip revert must drop — is required and tied to
    unrecognized/typo input."""
    text = SKILL.read_text(encoding="utf-8")
    # Normalize whitespace so multi-line / markdown wrapping never breaks the
    # proximity matches below (collapse all runs of whitespace to one space).
    flat = re.sub(r"\s+", " ", text.lower())

    # ── POSITIVE: a co-located phrase that Enter APPLIES/WRITES the
    # cost-effective profile. ALL branches require `enter`, an apply/write verb,
    # AND `cost-effective` within a bounded window — so a bare "press enter"
    # elsewhere, or generic "apply ... cost-effective" prose without `enter`,
    # does NOT satisfy it (the reviewer's bare-substring weakness).
    apply_verb = r"(?:appl(?:y|ies|ied)|writes?)"
    enter_applies_cost = re.search(
        rf"enter\b[^.]{{0,80}}?\b{apply_verb}\b[^.]{{0,80}}?cost-effective"
        rf"|cost-effective[^.]{{0,80}}?\b{apply_verb}\b[^.]{{0,80}}?\benter\b",
        flat,
    )
    assert enter_applies_cost is not None, (
        "menu must state that pressing Enter APPLIES/WRITES the cost-effective "
        "profile (co-located 'enter' + apply/write + cost-effective phrase missing)"
    )
    # The informed-consent statement: Enter WRITES the posture to settings.json.
    # (Bound the gaps but allow '.' so the '~/.claude/settings.json' path itself
    # does not break the window; stop at sentence boundaries via newline-free.)
    assert re.search(r"enter\b[^\n]{0,120}?writes?\b[^\n]{0,120}?settings\.json", flat), (
        "menu lost the informed-consent note that Enter WRITES the posture to "
        "~/.claude/settings.json"
    )

    # ── NEGATIVE (phrasing-agnostic): the default input (enter / empty / blank /
    # "no answer") must NOT be co-located with a skip/decline/keep-current/
    # leave-untouched/write-nothing verb. This is the load-bearing guard — it
    # catches a differently-worded empty=skip revert ("a blank answer skips",
    # "a blank reply keeps your current settings unchanged", "empty ⇒ leaves
    # settings untouched", etc.), not just the exact old phrase. The skip/leave
    # verb is matched flexibly (optional intervening words like "your"/"the").
    skip_verb = (
        r"(?:skips?|declines?|"
        r"keeps?\b[^.]{0,20}?\bcurrent|"  # "keeps current" / "keeps your current"
        r"leaves?\b[^.]{0,20}?\b(?:untouched|unchanged|alone|as-is|as is)|"
        r"(?:settings|them|it)\b[^.]{0,20}?\b(?:untouched|unchanged)|"
        r"write[s]?\b[^.]{0,12}?\bnothing|"
        r"nothing\b[^.]{0,12}?\bwritten|"
        r"no\b[^.]{0,12}?\b(?:settings? write|change(?:s|d)?))"
    )
    empty_skips = re.search(
        r"\b(?:enter|empty|blank|no answer|no reply)\b[^.]{0,70}?" + skip_verb,
        flat,
    )
    assert empty_skips is None, (
        "menu still ties Enter/empty/blank to SKIP/decline/keep-current/"
        "leave-untouched/write-nothing — a differently-worded empty=skip revert "
        f"leaked through: matched {empty_skips.group(0)!r}"
    )

    # ── Explicit skip still writes nothing (the option must remain).
    assert re.search(
        r"skip\b[^.]{0,60}?(?:write[s]? nothing|keep[s]? current|no settings)", flat
    ) or ("write nothing" in flat or "writes nothing" in flat or "no settings write" in flat), (
        "menu lost the explicit-skip-writes-nothing guarantee"
    )

    # ── Re-ask-once guard tied to unrecognized/typo input (the safeguard a real
    # empty=skip revert would drop, since that revert auto-skips on any non-match).
    assert re.search(
        r"(?:unrecognized|ambiguous|typo)[^\n]{0,200}?\b(?:re-?ask|re-?present)\b"
        r"|\b(?:re-?ask|re-?present)\b[^\n]{0,200}?(?:unrecognized|ambiguous|typo)",
        flat,
    ), (
        "procedure lost the re-ask-once guard tying unrecognized/typo input to a re-ask (no auto-write)"
    )


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
    """CLAUDE.md '## Model recommendations' documents the opt-in lever: the two
    named profiles (cost-effective default + code-quality), the load-bearing
    posture words, the subagent-model env mechanism + the subagent-effort
    limitation, and the consent/opt-in + once-per-version semantics."""
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## Model recommendations" in text
    # The two named profiles (cost-effective is the default/recommended).
    assert "cost-effective" in text
    assert "code-quality" in text
    # Load-bearing posture words (pinned across the refactor).
    assert "sonnet" in text
    assert "effortLevel" in text
    assert "autoCompactEnabled" in text
    # The subagent-model env mechanism + the subagent-effort limitation.
    assert "CLAUDE_CODE_SUBAGENT_MODEL" in text
    lowered = text.lower()
    assert "subagent effort" in lowered or "subagent-effort" in lowered
    # Governance words for this lever (consent/opt-in + once-per-version).
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
