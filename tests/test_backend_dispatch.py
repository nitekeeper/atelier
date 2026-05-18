"""Plan 2 Task 9 — facade dispatch tests for `scripts/backend.py`.

Verifies that the facade routes each implemented method to either
`backend_memex` or `backend_local` based on `mode_detector.detect_mode()`,
and that `domain` is validated BEFORE either backend is invoked
(defense-in-depth — both backends also validate, but the facade catches
it first so callers see a hermetic ValueError without touching either
SQLite/Memex import path).

The six deferred methods (`find_or_create_workspace`,
`find_workspace_by_identity`, `list_workspaces`, `find_project`,
`list_projects`, `get_document`) are exercised in `test_backend_skeleton.py`
to confirm they still raise NotImplementedError — spec §4.3 keeps them on
the surface but Plan 2 defers their implementation to v1.2.0.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts import backend, mode_detector


@pytest.fixture(autouse=True)
def _clear_mode_cache():
    """Reset `detect_mode` cache before AND after each test so a test that
    forces the cache via a fixture/patch doesn't leak into the next."""
    mode_detector._clear_cache()
    yield
    mode_detector._clear_cache()


# ── Core dispatch behaviour ────────────────────────────────────────────────


def test_facade_routes_to_memex_when_mode_is_memex():
    """When `detect_mode() == "memex"`, the facade delegates to
    `backend_memex.write_document` with the spec §4.3 kwargs."""
    with patch.object(mode_detector, "detect_mode", return_value="memex"), \
         patch("scripts.backend_memex.write_document",
               return_value={"row_id": 1, "index_id": "i-1"}) as memex_call, \
         patch("scripts.backend_local.write_document") as local_call:
        result = backend.write_document(
            workspace_id=1, project_id=2, domain="design",
            subdomain="auth", title="Auth Design", body="OAuth2 plan",
            metadata={"k": "v"}, caller_agent_id="atelier-pm-1",
        )
    assert result == {"row_id": 1, "index_id": "i-1"}
    memex_call.assert_called_once()
    local_call.assert_not_called()


def test_facade_routes_to_local_when_mode_is_local():
    """When `detect_mode() == "local"`, the facade delegates to
    `backend_local.write_document` with the spec §4.3 kwargs."""
    with patch.object(mode_detector, "detect_mode", return_value="local"), \
         patch("scripts.backend_local.write_document",
               return_value={"row_id": 7, "index_id": None}) as local_call, \
         patch("scripts.backend_memex.write_document") as memex_call:
        result = backend.write_document(
            workspace_id=1, project_id=2, domain="design",
            subdomain="auth", title="Auth Design", body="OAuth2 plan",
            metadata={"k": "v"}, caller_agent_id="atelier-pm-1",
        )
    assert result == {"row_id": 7, "index_id": None}
    local_call.assert_called_once()
    memex_call.assert_not_called()


def test_facade_assert_valid_domain_before_dispatch():
    """An unknown `domain` raises `ValueError` BEFORE either backend is
    called. Belt-and-suspenders: both backends validate independently,
    but catching at the facade keeps the unknown-domain path hermetic
    (no Memex config / SQLite open) so callers see a clean error."""
    with patch.object(mode_detector, "detect_mode", return_value="memex"), \
         patch("scripts.backend_memex.write_document") as memex_call, \
         patch("scripts.backend_local.write_document") as local_call:
        with pytest.raises(ValueError):
            backend.write_document(
                workspace_id=1, project_id=2, domain="garbage",
                subdomain=None, title="t", body="b",
                metadata={}, caller_agent_id="a",
            )
    memex_call.assert_not_called()
    local_call.assert_not_called()


# ── Every dispatched method routes correctly ───────────────────────────────


def _patch_all_local(**overrides):
    """Helper: monkey-patch every backend_local symbol the facade dispatches
    to with a `lambda **k: marker` so we can assert which method was hit
    without firing real SQLite I/O. `overrides` lets callers swap a specific
    method to return a richer value when they need it."""
    defaults = {
        "write_project":   lambda **k: {"id": 1, "marker": "write_project"},
        "write_document":  lambda **k: {"id": 1, "marker": "write_document"},
        "write_task":      lambda **k: {"id": 1, "marker": "write_task"},
        "write_meeting":   lambda **k: {"id": 1, "marker": "write_meeting"},
        "upsert_session":  lambda **k: {"id": 1, "marker": "upsert_session"},
        "transition_phase": lambda **k: {"id": 1, "marker": "transition_phase"},
        "update_task_status": lambda **k: {"id": 1, "marker": "update_task_status"},
        "record_phase_bypass": lambda **k: {"id": 1, "marker": "record_phase_bypass"},
        "find_documents":  lambda **k: [{"marker": "find_documents"}],
        "get_task":        lambda **k: {"marker": "get_task"},
        "list_tasks":      lambda **k: [{"marker": "list_tasks"}],
        "lookup_index_id_by_source_ref": lambda **k: "lookup-id",
        "find_or_create_role":  lambda **k: {"marker": "find_or_create_role"},
        "find_or_create_agent": lambda **k: {"marker": "find_or_create_agent"},
    }
    defaults.update(overrides)
    return defaults


def test_every_implemented_method_dispatches_to_local():
    """For each of the 14 spec §4.3 methods the facade implements, calling
    the facade in local mode reaches the matching `backend_local` symbol
    (proven via a unique marker per method). Acts as the comprehensive
    "no method left as a stub" guard."""
    patches = _patch_all_local()
    with patch.object(mode_detector, "detect_mode", return_value="local"), \
         patch.multiple("scripts.backend_local", **patches):
        assert backend.write_project(
            workspace_id=1, slug="p", name="P",
            description="d", created_by="a")["marker"] == "write_project"
        assert backend.write_document(
            workspace_id=1, project_id=1, domain="design", subdomain=None,
            title="t", body="b", metadata={},
            caller_agent_id="a")["marker"] == "write_document"
        assert backend.write_task(
            workspace_id=1, project_id=1, title="t", description="d",
            subdomain=None, created_by="a")["marker"] == "write_task"
        assert backend.write_meeting(
            workspace_id=1, project_id=1, title="t",
            date="2026-05-18", summary="s", decisions="d",
            subdomain=None, created_by="a")["marker"] == "write_meeting"
        assert backend.upsert_session(
            project_id=1, agent_id="a")["marker"] == "upsert_session"
        assert backend.transition_phase(
            project_id=1, to_phase="plan:open",
            agent_id="a")["marker"] == "transition_phase"
        assert backend.update_task_status(
            task_id=1, status="done")["marker"] == "update_task_status"
        assert backend.record_phase_bypass(
            project_id=1, from_phase="x", to_phase="y",
            reason="r", agent_id="a")["marker"] == "record_phase_bypass"
        assert backend.find_documents(query="q")[0]["marker"] == "find_documents"
        assert backend.get_task(task_id=1)["marker"] == "get_task"
        assert backend.list_tasks(project_id=1)[0]["marker"] == "list_tasks"
        assert backend.lookup_index_id_by_source_ref(
            source_ref="atelier:tasks:1") == "lookup-id"
        assert backend.find_or_create_role(
            name="PM", description="d")["marker"] == "find_or_create_role"
        assert backend.find_or_create_agent(
            agent_id="x", name="X", role_id=1,
            profile="p")["marker"] == "find_or_create_agent"


# ── Memex-mode signature adapter ───────────────────────────────────────────
#
# Wave 0 `backend.py` exposes the spec §4.3 wide signature (e.g.
# `write_document(*, workspace_id, project_id, domain, subdomain, …)`),
# but `backend_memex.write_document` is narrower — `workspace_id` /
# `project_id` / `subdomain` are folded into `metadata` for the Tier 2
# librarian_output. The facade adapts: the wider Wave 0 signature is
# stable for callers, and the adapter unpacks into `metadata` before
# delegating to `backend_memex`.


def test_facade_folds_wide_kwargs_into_metadata_for_memex():
    """When dispatching to memex, the facade must move the wide-signature
    extras (`workspace_id`, `project_id`, `subdomain`) into `metadata` so
    the narrower `backend_memex.write_document` signature accepts them."""
    captured = {}

    def fake_write_document(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with patch.object(mode_detector, "detect_mode", return_value="memex"), \
         patch("scripts.backend_memex.write_document", new=fake_write_document):
        backend.write_document(
            workspace_id=42, project_id=7, domain="adr",
            subdomain="security", title="ADR-007",
            body="Use OAuth2 over SAML.",
            metadata={"existing": "value"},
            caller_agent_id="atelier-pm-1",
            source_url="https://example/adr-007",
        )

    # Wide-signature kwargs not in backend_memex.write_document's signature
    # must arrive folded into metadata.
    assert "workspace_id" not in captured
    assert "subdomain" not in captured
    assert captured["metadata"]["workspace_id"] == 42
    assert captured["metadata"]["project_id"] == 7
    assert captured["metadata"]["subdomain"] == "security"
    # Caller-supplied metadata survives the fold.
    assert captured["metadata"]["existing"] == "value"
    # The narrow-signature kwargs ride through unchanged.
    assert captured["domain"] == "adr"
    assert captured["title"] == "ADR-007"
    assert captured["caller_agent_id"] == "atelier-pm-1"
    assert captured["source_url"] == "https://example/adr-007"


def test_facade_folds_wide_kwargs_into_metadata_for_write_task_memex():
    """Same adapter contract for `write_task`: `workspace_id` and
    `subdomain` are not in `backend_memex.write_task`'s signature, so the
    facade folds them into metadata before delegating."""
    captured = {}

    def fake_write_task(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with patch.object(mode_detector, "detect_mode", return_value="memex"), \
         patch("scripts.backend_memex.write_task", new=fake_write_task):
        backend.write_task(
            workspace_id=10, project_id=3, title="Add OAuth refresh",
            description="…", subdomain="auth", created_by="atelier-eng-1",
            assigned_to="atelier-eng-2", priority=5, notes="some notes",
        )

    assert "workspace_id" not in captured
    assert "subdomain" not in captured
    # backend_memex.write_task doesn't accept `metadata` — the adapter must
    # NOT pass it through. The narrow signature accepts the rest as-is.
    assert "metadata" not in captured
    assert captured["project_id"] == 3
    assert captured["title"] == "Add OAuth refresh"
    assert captured["assigned_to"] == "atelier-eng-2"
    assert captured["priority"] == 5
    assert captured["notes"] == "some notes"


def test_facade_folds_wide_kwargs_into_metadata_for_write_meeting_memex():
    """`write_meeting` adapter: `workspace_id` and `subdomain` are folded
    out for the memex backend; everything else passes through."""
    captured = {}

    def fake_write_meeting(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with patch.object(mode_detector, "detect_mode", return_value="memex"), \
         patch("scripts.backend_memex.write_meeting", new=fake_write_meeting):
        backend.write_meeting(
            workspace_id=10, project_id=3, title="Sync",
            date="2026-05-18", summary="…", decisions="…",
            subdomain="weekly", created_by="atelier-pm-1",
        )

    assert "workspace_id" not in captured
    assert "subdomain" not in captured
    assert captured["title"] == "Sync"
    assert captured["date"] == "2026-05-18"
    assert captured["project_id"] == 3
