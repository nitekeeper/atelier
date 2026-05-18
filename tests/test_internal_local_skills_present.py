"""Smoke tests for Local-mode internal SKILL.md procedures.

Mirrors the pattern of T11's `test_internal_skills_present.py`. Each test
asserts the file exists and contains the routing-contract substrings that
local-mode agents will grep for. These markers double as a guarantee that
the recipe still names the canonical `backend_local.*` entry points and
the `.atelier/raw/` archive location.
"""
from pathlib import Path

INTERNAL_LOCAL = Path(__file__).parent.parent / "internal" / "local"


def test_wiki_write_skill_present():
    f = INTERNAL_LOCAL / "wiki-write" / "SKILL.md"
    assert f.exists(), f"Missing: {f}"
    text = f.read_text(encoding="utf-8")
    assert "backend_local.write_document" in text
    assert "FTS5" in text
    assert ".atelier/raw/" in text


def test_wiki_search_skill_present():
    f = INTERNAL_LOCAL / "wiki-search" / "SKILL.md"
    assert f.exists(), f"Missing: {f}"
    text = f.read_text(encoding="utf-8")
    assert "backend_local.find_documents" in text
    assert "FTS5" in text


def test_wiki_archive_skill_present():
    f = INTERNAL_LOCAL / "wiki-archive" / "SKILL.md"
    assert f.exists(), f"Missing: {f}"
    text = f.read_text(encoding="utf-8")
    assert ".atelier/raw/" in text
    assert "<canonical_key>" in text


def test_state_crud_skill_present():
    f = INTERNAL_LOCAL / "state-crud" / "SKILL.md"
    assert f.exists(), f"Missing: {f}"
    text = f.read_text(encoding="utf-8")
    assert "backend_local.upsert_session" in text
    assert "backend_local.update_task_status" in text
    assert "backend_local.record_phase_bypass" in text
