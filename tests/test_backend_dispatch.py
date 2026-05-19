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

# `_clear_mode_cache` is now an autouse fixture in `tests/conftest.py`
# so every facade-touching test file gets the same hermetic-mode guarantee
# without duplicating the fixture per-file.


# ── Core dispatch behaviour ────────────────────────────────────────────────


def test_facade_routes_to_memex_when_mode_is_memex():
    """When `detect_mode() == "memex"`, the facade delegates to
    `backend_memex.write_document` with the spec §4.3 kwargs.

    N6: spot-check the forwarded kwargs (domain / title / caller_agent_id)
    so this doesn't degenerate into an "any call counts" smoke test.
    """
    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch(
            "scripts.backend_memex.write_document", return_value={"row_id": 1, "index_id": "i-1"}
        ) as memex_call,
        patch("scripts.backend_local.write_document") as local_call,
    ):
        result = backend.write_document(
            workspace_id=1,
            project_id=2,
            domain="design",
            subdomain="auth",
            title="Auth Design",
            body="OAuth2 plan",
            metadata={"k": "v"},
            caller_agent_id="atelier-pm-1",
        )
    assert result == {"row_id": 1, "index_id": "i-1"}
    memex_call.assert_called_once()
    # Spot-check forwarded kwargs (N6) — adapter folds workspace_id /
    # subdomain into metadata, but the narrow-signature kwargs ride
    # through unchanged.
    _, called_kwargs = memex_call.call_args
    assert called_kwargs["domain"] == "design"
    assert called_kwargs["title"] == "Auth Design"
    assert called_kwargs["caller_agent_id"] == "atelier-pm-1"
    local_call.assert_not_called()


def test_facade_routes_to_local_when_mode_is_local():
    """When `detect_mode() == "local"`, the facade delegates to
    `backend_local.write_document` with the spec §4.3 kwargs.

    N6: spot-check forwarded kwargs — Local is a pure pass-through so
    workspace_id / subdomain / domain all arrive unchanged.
    """
    with (
        patch.object(mode_detector, "detect_mode", return_value="local"),
        patch(
            "scripts.backend_local.write_document", return_value={"row_id": 7, "index_id": None}
        ) as local_call,
        patch("scripts.backend_memex.write_document") as memex_call,
    ):
        result = backend.write_document(
            workspace_id=1,
            project_id=2,
            domain="design",
            subdomain="auth",
            title="Auth Design",
            body="OAuth2 plan",
            metadata={"k": "v"},
            caller_agent_id="atelier-pm-1",
        )
    assert result == {"row_id": 7, "index_id": None}
    local_call.assert_called_once()
    _, called_kwargs = local_call.call_args
    assert called_kwargs["workspace_id"] == 1
    assert called_kwargs["subdomain"] == "auth"
    assert called_kwargs["domain"] == "design"
    memex_call.assert_not_called()


def test_facade_assert_valid_domain_before_dispatch():
    """An unknown `domain` raises `ValueError` BEFORE either backend is
    called. Belt-and-suspenders: Memex re-validates independently, but
    catching at the facade keeps the unknown-domain path hermetic (no
    Memex config / SQLite open) so callers see a clean error.

    N8: pin the error message to "garbage" so a future refactor that
    swallows the diagnostic (or rewrites it to "invalid input") fails
    this test loudly rather than silently passing.
    """
    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_document") as memex_call,
        patch("scripts.backend_local.write_document") as local_call,
    ):
        with pytest.raises(ValueError, match="garbage"):
            backend.write_document(
                workspace_id=1,
                project_id=2,
                domain="garbage",
                subdomain=None,
                title="t",
                body="b",
                metadata={},
                caller_agent_id="a",
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
        "write_project": lambda **k: {"id": 1, "marker": "write_project"},
        "write_document": lambda **k: {"id": 1, "marker": "write_document"},
        "write_task": lambda **k: {"id": 1, "marker": "write_task"},
        "write_meeting": lambda **k: {"id": 1, "marker": "write_meeting"},
        "upsert_session": lambda **k: {"id": 1, "marker": "upsert_session"},
        "transition_phase": lambda **k: {"id": 1, "marker": "transition_phase"},
        "update_task_status": lambda **k: {"id": 1, "marker": "update_task_status"},
        "record_phase_bypass": lambda **k: {"id": 1, "marker": "record_phase_bypass"},
        "find_documents": lambda **k: [{"marker": "find_documents"}],
        "get_task": lambda **k: {"marker": "get_task"},
        "list_tasks": lambda **k: [{"marker": "list_tasks"}],
        "lookup_index_id_by_source_ref": lambda **k: "lookup-id",
        "find_or_create_role": lambda **k: {"marker": "find_or_create_role"},
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
    with (
        patch.object(mode_detector, "detect_mode", return_value="local"),
        patch.multiple("scripts.backend_local", **patches),
    ):
        assert (
            backend.write_project(
                workspace_id=1, slug="p", name="P", description="d", created_by="a"
            )["marker"]
            == "write_project"
        )
        assert (
            backend.write_document(
                workspace_id=1,
                project_id=1,
                domain="design",
                subdomain=None,
                title="t",
                body="b",
                metadata={},
                caller_agent_id="a",
            )["marker"]
            == "write_document"
        )
        assert (
            backend.write_task(
                workspace_id=1,
                project_id=1,
                title="t",
                description="d",
                subdomain=None,
                created_by="a",
            )["marker"]
            == "write_task"
        )
        assert (
            backend.write_meeting(
                workspace_id=1,
                project_id=1,
                title="t",
                date="2026-05-18",
                summary="s",
                decisions="d",
                subdomain=None,
                created_by="a",
            )["marker"]
            == "write_meeting"
        )
        assert backend.upsert_session(project_id=1, agent_id="a")["marker"] == "upsert_session"
        assert (
            backend.transition_phase(project_id=1, to_phase="plan:open", agent_id="a")["marker"]
            == "transition_phase"
        )
        assert (
            backend.update_task_status(task_id=1, status="done")["marker"] == "update_task_status"
        )
        assert (
            backend.record_phase_bypass(
                project_id=1, from_phase="x", to_phase="y", reason="r", agent_id="a"
            )["marker"]
            == "record_phase_bypass"
        )
        assert backend.find_documents(query="q")[0]["marker"] == "find_documents"
        assert backend.get_task(task_id=1)["marker"] == "get_task"
        assert backend.list_tasks(project_id=1)[0]["marker"] == "list_tasks"
        assert backend.lookup_index_id_by_source_ref(source_ref="atelier:tasks:1") == "lookup-id"
        assert (
            backend.find_or_create_role(name="PM", description="d")["marker"]
            == "find_or_create_role"
        )
        assert (
            backend.find_or_create_agent(agent_id="x", name="X", role_id=1, profile="p")["marker"]
            == "find_or_create_agent"
        )


def _patch_all_memex(**overrides):
    """Memex-mode mirror of `_patch_all_local`. Methods that the facade
    adapts (write_document / write_task / write_meeting) still get
    unique markers so the dispatch check holds — the adapter folds extra
    kwargs into metadata but the underlying call still lands here."""
    defaults = {
        "write_project": lambda **k: {"id": 1, "marker": "write_project"},
        "write_document": lambda **k: {"id": 1, "marker": "write_document"},
        "write_task": lambda **k: {"id": 1, "marker": "write_task"},
        "write_meeting": lambda **k: {"id": 1, "marker": "write_meeting"},
        "upsert_session": lambda **k: {"id": 1, "marker": "upsert_session"},
        "transition_phase": lambda **k: {"id": 1, "marker": "transition_phase"},
        "update_task_status": lambda **k: {"id": 1, "marker": "update_task_status"},
        "record_phase_bypass": lambda **k: {"id": 1, "marker": "record_phase_bypass"},
        "find_documents": lambda **k: [{"marker": "find_documents"}],
        "get_task": lambda **k: {"marker": "get_task"},
        "list_tasks": lambda **k: [{"marker": "list_tasks"}],
        "lookup_index_id_by_source_ref": lambda **k: "lookup-id",
        "find_or_create_role": lambda **k: {"marker": "find_or_create_role"},
        "find_or_create_agent": lambda **k: {"marker": "find_or_create_agent"},
    }
    defaults.update(overrides)
    return defaults


def test_every_implemented_method_dispatches_to_memex():
    """I3: Memex-mode mirror of `_to_local`. Pre-T16 only 3/14 methods
    were covered for memex dispatch (the three signature-adapter folds);
    this test pins the remaining 11 — write_project, upsert_session,
    transition_phase, update_task_status, record_phase_bypass,
    find_documents, get_task, list_tasks, lookup_index_id_by_source_ref,
    find_or_create_role, find_or_create_agent — so a future refactor
    that flips the dispatch table fails loudly per-method."""
    patches = _patch_all_memex()
    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch.multiple("scripts.backend_memex", **patches),
    ):
        assert (
            backend.write_project(
                workspace_id=1, slug="p", name="P", description="d", created_by="a"
            )["marker"]
            == "write_project"
        )
        assert (
            backend.write_document(
                workspace_id=1,
                project_id=1,
                domain="design",
                subdomain=None,
                title="t",
                body="b",
                metadata={},
                caller_agent_id="a",
            )["marker"]
            == "write_document"
        )
        assert (
            backend.write_task(
                workspace_id=1,
                project_id=1,
                title="t",
                description="d",
                subdomain=None,
                created_by="a",
            )["marker"]
            == "write_task"
        )
        assert (
            backend.write_meeting(
                workspace_id=1,
                project_id=1,
                title="t",
                date="2026-05-18",
                summary="s",
                decisions="d",
                subdomain=None,
                created_by="a",
            )["marker"]
            == "write_meeting"
        )
        assert backend.upsert_session(project_id=1, agent_id="a")["marker"] == "upsert_session"
        assert (
            backend.transition_phase(project_id=1, to_phase="plan:open", agent_id="a")["marker"]
            == "transition_phase"
        )
        assert (
            backend.update_task_status(task_id=1, status="done")["marker"] == "update_task_status"
        )
        assert (
            backend.record_phase_bypass(
                project_id=1, from_phase="x", to_phase="y", reason="r", agent_id="a"
            )["marker"]
            == "record_phase_bypass"
        )
        assert backend.find_documents(query="q")[0]["marker"] == "find_documents"
        assert backend.get_task(task_id=1)["marker"] == "get_task"
        assert backend.list_tasks(project_id=1)[0]["marker"] == "list_tasks"
        assert backend.lookup_index_id_by_source_ref(source_ref="atelier:tasks:1") == "lookup-id"
        assert (
            backend.find_or_create_role(name="PM", description="d")["marker"]
            == "find_or_create_role"
        )
        assert (
            backend.find_or_create_agent(agent_id="x", name="X", role_id=1, profile="p")["marker"]
            == "find_or_create_agent"
        )


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
    extras (`workspace_id`, `project_id`, `subdomain`, `source_ref`) into
    `metadata` so the narrower `backend_memex.write_document` signature
    accepts them — `source_ref` in particular has no positional slot on
    the Memex backend and must persist via the metadata blob (I1)."""
    captured = {}

    def fake_write_document(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_document", new=fake_write_document),
    ):
        backend.write_document(
            workspace_id=42,
            project_id=7,
            domain="adr",
            subdomain="security",
            title="ADR-007",
            body="Use OAuth2 over SAML.",
            metadata={"existing": "value"},
            caller_agent_id="atelier-pm-1",
            source_url="https://example/adr-007",
            source_ref="atelier:documents:7",
        )

    # Wide-signature kwargs not in backend_memex.write_document's signature
    # must arrive folded into metadata.
    assert "workspace_id" not in captured
    assert "subdomain" not in captured
    assert "source_ref" not in captured
    assert captured["metadata"]["workspace_id"] == 42
    assert captured["metadata"]["project_id"] == 7
    assert captured["metadata"]["subdomain"] == "security"
    assert captured["metadata"]["source_ref"] == "atelier:documents:7"
    # Caller-supplied metadata survives the fold.
    assert captured["metadata"]["existing"] == "value"
    # The narrow-signature kwargs ride through unchanged.
    assert captured["domain"] == "adr"
    assert captured["title"] == "ADR-007"
    assert captured["caller_agent_id"] == "atelier-pm-1"
    assert captured["source_url"] == "https://example/adr-007"


def test_facade_omits_subdomain_from_metadata_when_none():
    """N4: `setdefault("subdomain", ...)` is gated on `subdomain is not
    None` so the metadata blob doesn't pick up a stray None entry that
    would later confuse subdomain-scoped FTS queries."""
    captured = {}

    def fake_write_document(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_document", new=fake_write_document),
    ):
        backend.write_document(
            workspace_id=1,
            project_id=1,
            domain="design",
            subdomain=None,
            title="t",
            body="b",
            metadata={},
            caller_agent_id="a",
        )

    assert "subdomain" not in captured["metadata"]


def test_facade_metadata_setdefault_lets_caller_win():
    """N5: setdefault precedence — caller-supplied `workspace_id` in
    `metadata` wins over the facade kwarg. Documents the "caller knows
    best" semantic so future maintainers don't flip it to assignment."""
    captured = {}

    def fake_write_document(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_document", new=fake_write_document),
    ):
        backend.write_document(
            workspace_id=42,
            project_id=7,
            domain="design",
            subdomain=None,
            title="t",
            body="b",
            metadata={"workspace_id": 99},
            caller_agent_id="a",
        )

    # Caller's 99 wins over the kwarg's 42.
    assert captured["metadata"]["workspace_id"] == 99


def test_facade_folds_wide_kwargs_into_metadata_for_write_task_memex():
    """Same adapter contract for `write_task`: `workspace_id` is dropped
    (no DB column; singleton workspace for now), `subdomain` is folded
    into the metadata blob (I4) so it survives into the Memex Index
    rather than getting silently discarded."""
    captured = {}

    def fake_write_task(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_task", new=fake_write_task),
    ):
        backend.write_task(
            workspace_id=10,
            project_id=3,
            title="Add OAuth refresh",
            description="…",
            subdomain="auth",
            created_by="atelier-eng-1",
            assigned_to="atelier-eng-2",
            priority=5,
            notes="some notes",
        )

    assert "workspace_id" not in captured
    assert "subdomain" not in captured
    # subdomain MUST land in metadata so the Memex Index row carries it
    # — the narrow `tasks` table has no subdomain column, but the
    # metadata blob is the canonical search slot.
    assert captured["metadata"] == {"subdomain": "auth"}
    assert captured["project_id"] == 3
    assert captured["title"] == "Add OAuth refresh"
    assert captured["assigned_to"] == "atelier-eng-2"
    assert captured["priority"] == 5
    assert captured["notes"] == "some notes"


def test_facade_write_task_memex_omits_metadata_when_subdomain_none():
    """N4 mirror for `write_task`: when `subdomain=None`, the facade
    passes `metadata=None` so the Memex backend's internally-built
    metadata isn't merged with an empty dict (cheaper) and a maintainer
    grepping for `"metadata":` in the captured call sees the absence."""
    captured = {}

    def fake_write_task(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_task", new=fake_write_task),
    ):
        backend.write_task(
            workspace_id=10,
            project_id=3,
            title="t",
            description="…",
            subdomain=None,
            created_by="a",
        )

    assert captured["metadata"] is None


def test_facade_folds_wide_kwargs_into_metadata_for_write_meeting_memex():
    """`write_meeting` adapter: `workspace_id` is dropped on the memex
    path; `subdomain` is folded into metadata (I4) so it survives into
    the Memex Index even though `meeting_minutes` has no subdomain
    column."""
    captured = {}

    def fake_write_meeting(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_meeting", new=fake_write_meeting),
    ):
        backend.write_meeting(
            workspace_id=10,
            project_id=3,
            title="Sync",
            date="2026-05-18",
            summary="…",
            decisions="…",
            subdomain="weekly",
            created_by="atelier-pm-1",
        )

    assert "workspace_id" not in captured
    assert "subdomain" not in captured
    assert captured["metadata"] == {"subdomain": "weekly"}
    assert captured["title"] == "Sync"
    assert captured["date"] == "2026-05-18"
    assert captured["project_id"] == 3


def test_facade_write_meeting_memex_omits_metadata_when_subdomain_none():
    """N4 mirror for `write_meeting`."""
    captured = {}

    def fake_write_meeting(**kwargs):
        captured.update(kwargs)
        return {"row_id": 1, "index_id": "i-1"}

    with (
        patch.object(mode_detector, "detect_mode", return_value="memex"),
        patch("scripts.backend_memex.write_meeting", new=fake_write_meeting),
    ):
        backend.write_meeting(
            workspace_id=10,
            project_id=3,
            title="t",
            date="2026-05-18",
            summary="s",
            decisions="d",
            subdomain=None,
            created_by="a",
        )

    assert captured["metadata"] is None


# ── Local-mode subdomain pass-through (N2) ─────────────────────────────────
#
# Local mode is a thin pass-through — `subdomain` is a real DB column on
# `tasks` and `meeting_minutes` (not on the slim schema in some branches,
# but the signature accepts it). These tests pin that the facade does not
# accidentally drop / rewrite it on the Local path.


def test_local_preserves_subdomain_for_write_task():
    """N2: Local mode must pass `subdomain` unchanged to
    `backend_local.write_task`."""
    captured = {}

    def fake_write_task(**kwargs):
        captured.update(kwargs)
        return {"id": 1}

    with (
        patch.object(mode_detector, "detect_mode", return_value="local"),
        patch("scripts.backend_local.write_task", new=fake_write_task),
    ):
        backend.write_task(
            workspace_id=1,
            project_id=2,
            title="t",
            description="d",
            subdomain="auth",
            created_by="a",
        )

    assert captured["subdomain"] == "auth"
    assert captured["workspace_id"] == 1
    assert captured["project_id"] == 2


def test_local_preserves_subdomain_for_write_meeting():
    """N2: Local mode must pass `subdomain` unchanged to
    `backend_local.write_meeting`."""
    captured = {}

    def fake_write_meeting(**kwargs):
        captured.update(kwargs)
        return {"id": 1}

    with (
        patch.object(mode_detector, "detect_mode", return_value="local"),
        patch("scripts.backend_local.write_meeting", new=fake_write_meeting),
    ):
        backend.write_meeting(
            workspace_id=1,
            project_id=2,
            title="t",
            date="2026-05-18",
            summary="s",
            decisions="d",
            subdomain="weekly",
            created_by="a",
        )

    assert captured["subdomain"] == "weekly"
    assert captured["workspace_id"] == 1
