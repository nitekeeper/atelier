"""Regression tests for the per-spawn rules-block token reduction.

`scripts/dispatch.py::_read_rules_block` strips worker-irrelevant boilerplate
(YAML frontmatter, HTML maintainer comments, and the ## CHANGELOG section) from
the team-mode rules text BEFORE it is injected into every worker briefing. The
goal is to stop paying tokens — on every spawn, and re-read from cache on every
worker turn — for content the workers never act on.

These tests pin two contracts:
  1. The strip removes the boilerplate AND meaningfully shrinks the block.
  2. The strip is behaviour-preserving: every load-bearing surface a worker
     acts on (TM-001..TM-008, the reply-envelope schema, the abandon grammar,
     the untrusted fence, the context-budget guidance, the Loom opt-out) still
     appears in the injected text.
  3. The on-disk SKILL.md is left untouched — it remains the source of truth
     for the file-reading tests and for pm_dispatch_envelope.py's ABANDON_RE.
"""

from scripts.dispatch import (
    RULES_SKILL,
    _read_rules_block,
    _strip_worker_irrelevant_rules,
)


def _raw() -> str:
    return RULES_SKILL.read_text(encoding="utf-8")


def test_boilerplate_removed_from_injected_block():
    injected = _read_rules_block()
    # HTML maintainer comment (the file itself says "not rendered to workers").
    assert "not rendered to workers" not in injected
    assert "Kleppmann" not in injected
    # CHANGELOG version rows.
    assert "| 1.3.1" not in injected
    assert "| 1.0" not in injected
    assert "## CHANGELOG" not in injected
    # Frontmatter metadata removed (the block; note "schema_version: 1" also
    # legitimately appears in TM-007's rule body, which must stay — so we pin
    # the frontmatter-only markers instead of that shared substring).
    assert not injected.startswith("---")
    assert "version: 1.3.2" not in injected
    assert "description: Team-mode hard rules" not in injected


def test_load_bearing_content_preserved():
    injected = _read_rules_block()
    # The eight hard rules.
    for n in range(1, 9):
        assert f"TM-00{n}" in injected
    # Abandon grammar (parsed by PM; must survive in the worker contract).
    assert "^ABANDON: (?P<category>" in injected
    # Reply envelope schema.
    assert "task_result" in injected
    # Untrusted-input fence (kaizen#62 AI-5 injection backstop).
    assert "<untrusted source=" in injected
    # Context-budget discipline phrase.
    assert "accumulating past" in injected or "150000" in injected
    # Loom opt-out exact value + the bridge-exclusivity clause.
    assert "ATELIER_LOOM_COMMS=0" in injected
    assert "ride the" in injected and "ALWAYS" in injected
    # Structured tables must survive intact (guards against a future regex
    # change silently eating a table even while the headline tokens remain).
    assert "| Field" in injected  # reply-envelope field table
    assert "next_action" in injected  # last reply-envelope field
    assert "| Category" in injected  # abandon-grammar category table
    assert "stale_rules" in injected  # an abandon category name
    # Heartbeat clause + self-verify protocol.
    assert "30 seconds" in injected
    assert "Self-verify" in injected


def test_meaningful_reduction():
    raw = _raw()
    injected = _read_rules_block()
    removed = len(raw) - len(injected)
    assert removed >= 3000, f"expected >=3000 chars removed, got {removed}"


def test_on_disk_file_is_untouched():
    # The strip applies only to the injected copy; the file keeps its
    # frontmatter + CHANGELOG (relied on by test_context_budget_lever.py and
    # by pm_dispatch_envelope.py's raw-file ABANDON_RE parse).
    raw = _raw()
    assert "## CHANGELOG" in raw
    assert "| 1.3.1" in raw
    assert "schema_version: 1" in raw


def test_strip_is_idempotent():
    injected = _read_rules_block()
    assert _strip_worker_irrelevant_rules(injected) == injected
