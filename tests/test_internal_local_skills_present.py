"""Smoke tests for Local-mode internal SKILL.md procedures.

Mirrors the pattern of T11's `test_internal_skills_present.py`. Each test
asserts the file exists and contains the routing-contract substrings that
local-mode agents will grep for. These markers double as a guarantee that
the recipe still names the canonical `backend_local.*` entry points and
the `.ai/raw/` archive location (per spec §4/§7/§10.4 — NOT `.atelier/raw/`).
"""
from pathlib import Path

INTERNAL_LOCAL = Path(__file__).parent.parent / "internal" / "local"


def _assert_markers(filename: str, markers: list[str]) -> None:
    f = INTERNAL_LOCAL / filename / "SKILL.md"
    assert f.exists(), f"Missing: {f}"
    text = f.read_text(encoding="utf-8")
    for marker in markers:
        assert marker in text, f"{filename} SKILL.md missing marker: {marker!r}"


def test_wiki_write_skill_present():
    _assert_markers(
        "wiki-write",
        [
            "backend_local.write_document",
            "FTS5",
            ".ai/raw/",
        ],
    )


def test_wiki_search_skill_present():
    _assert_markers(
        "wiki-search",
        [
            "backend_local.find_documents",
            "FTS5",
        ],
    )


def test_wiki_archive_skill_present():
    _assert_markers(
        "wiki-archive",
        [
            ".ai/raw/",
            # N3: <canonical_key> retained as a passing reference at the top
            # for grep continuity; <archive_basename> is the operative term
            # used for the filename component throughout.
            "<canonical_key>",
            "<archive_basename>",
        ],
    )


def test_state_crud_skill_present():
    _assert_markers(
        "state-crud",
        [
            "backend_local.upsert_session",
            "backend_local.update_task_status",
            "backend_local.record_phase_bypass",
        ],
    )
