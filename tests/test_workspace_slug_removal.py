"""Workspace slug removal tests (atelier#55).

Verifies that the `_WORKSPACE_SLUG = "atelier"` hardcoding is gone and
that `_atelier_write` now derives the workspace slug from metadata or
the singleton workspace row.

Also verifies that the atelier#30 `_auto_relations` workspace_id filter
is now ACTIVE — `_atelier_write` injects `workspace_id` into the
metadata blob before `_auto_relations` runs, so the filter fires on
every write instead of being a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import backend_memex

# ── Constant removal ──────────────────────────────────────────────────────


def test_no_workspace_slug_constant_in_backend_memex():
    """Anti-regression: the `_WORKSPACE_SLUG` module attribute MUST NOT
    exist on `scripts.backend_memex` after atelier#55. Future code
    that reintroduces a hardcoded workspace slug fails this guard."""
    assert not hasattr(backend_memex, "_WORKSPACE_SLUG"), (
        "backend_memex._WORKSPACE_SLUG was reintroduced after atelier#55"
    )


def test_no_workspace_slug_literal_in_production_source():
    """Anti-regression: NO production scripts/* file should contain a
    `_WORKSPACE_SLUG` literal reference (constants, comments, or
    docstrings)."""
    scripts_dir = Path(__file__).parent.parent / "scripts"
    offenders = []
    for py in scripts_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "_WORKSPACE_SLUG" in text:
            offenders.append(str(py.relative_to(scripts_dir.parent)))
    assert offenders == [], f"_WORKSPACE_SLUG reference reintroduced in: {offenders}"


# ── _singleton_workspace + _workspace_slug_for_id behavior ────────────────


def test_singleton_workspace_returns_first_row_no_slug_filter(monkeypatch):
    """`_singleton_workspace` MUST query `workspaces` with NO `where`
    clause (post-#55 — pre-#55 it slug-filtered for "atelier"). The
    first row returned by Memex Core wins."""
    monkeypatch.undo()  # release the conftest autouse stub for this test
    captured: dict = {}

    def fake_query(*, store, table, where=None):
        captured["store"] = store
        captured["table"] = table
        captured["where"] = where
        return [{"id": 9, "slug": "renamed-workspace", "name": "Renamed"}]

    monkeypatch.setattr(backend_memex, "_memex_core_query", fake_query)
    row = backend_memex._singleton_workspace()
    assert captured == {"store": "atelier", "table": "workspaces", "where": None}
    assert row["id"] == 9
    assert row["slug"] == "renamed-workspace"


def test_singleton_workspace_raises_when_store_empty(monkeypatch):
    """No workspace row → RuntimeError pointing at the bootstrap step."""
    monkeypatch.undo()
    monkeypatch.setattr(backend_memex, "_memex_core_query", lambda **kw: [])
    with pytest.raises(RuntimeError, match="bootstrap"):
        backend_memex._singleton_workspace()


def test_workspace_slug_for_id_resolves_by_id(monkeypatch):
    """`_workspace_slug_for_id(N)` MUST query `workspaces` with
    `where={"id": N}` and return the row's slug. Post-#55 contract."""
    monkeypatch.undo()
    captured: dict = {}

    def fake_query(*, store, table, where=None):
        captured["where"] = where
        return [{"id": where["id"], "slug": "found-slug"}]

    monkeypatch.setattr(backend_memex, "_memex_core_query", fake_query)
    slug = backend_memex._workspace_slug_for_id(42)
    assert slug == "found-slug"
    assert captured["where"] == {"id": 42}


def test_workspace_slug_for_id_raises_for_unknown(monkeypatch):
    """Unknown workspace_id → ValueError. Same shape as
    `_workspace_id_for_project`'s bad-input error."""
    monkeypatch.undo()
    monkeypatch.setattr(backend_memex, "_memex_core_query", lambda **kw: [])
    with pytest.raises(ValueError, match="workspace_id=99"):
        backend_memex._workspace_slug_for_id(99)


# ── _atelier_write derives slug + injects workspace_id ────────────────────


def test_atelier_write_uses_singleton_slug_when_metadata_lacks_workspace_id(
    monkeypatch,
):
    """When metadata has no workspace_id, `_atelier_write` falls back to
    the singleton workspace. The resulting §6.7 key carries the
    singleton's slug — NOT a hardcoded `"atelier"` literal anymore."""
    monkeypatch.setattr(
        backend_memex,
        "_singleton_workspace",
        lambda: {"id": 5, "slug": "post-rename-slug"},
    )
    captured: dict = {}
    _stub_write_entry(monkeypatch, captured)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "proj")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    backend_memex.write_task(
        title="t",
        description="",
        project_id=1,
        created_by="atelier-engineer-1",
    )
    key = captured["librarian_output"]["key"]
    assert key.startswith("post-rename-slug/proj/task/"), key


def test_atelier_write_derives_slug_from_metadata_workspace_id(monkeypatch):
    """When metadata carries `workspace_id`, `_atelier_write` resolves
    the slug via `_workspace_slug_for_id` — supports multi-workspace
    deployments where the caller pre-resolved the workspace."""
    monkeypatch.setattr(
        backend_memex,
        "_workspace_slug_for_id",
        lambda workspace_id: f"resolved-{workspace_id}",
    )
    captured: dict = {}
    _stub_write_entry(monkeypatch, captured)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "proj")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    backend_memex.write_document(
        domain="design",
        title="x",
        body="b",
        metadata={"workspace_id": 7, "project_id": 1},
        caller_agent_id="atelier-engineer-1",
    )
    key = captured["librarian_output"]["key"]
    assert key.startswith("resolved-7/proj/design/"), key


def test_atelier_write_injects_workspace_id_into_metadata_when_absent(monkeypatch):
    """atelier#55 activates the atelier#30 filter: when metadata
    doesn't carry workspace_id, `_atelier_write` injects the singleton's
    id BEFORE calling `_auto_relations`. The filter then sees a
    non-None workspace_id and fires the SQL clause (today's single-
    workspace deployment makes it a no-op for correctness, but the
    plumbing is active).
    """
    monkeypatch.setattr(
        backend_memex,
        "_singleton_workspace",
        lambda: {"id": 11, "slug": "atelier"},
    )
    metadata_seen: dict = {}

    def spy_auto_relations(metadata, explicit):
        metadata_seen.update(metadata)
        return list(explicit or [])

    monkeypatch.setattr(backend_memex, "_auto_relations", spy_auto_relations)
    captured: dict = {}
    _stub_write_entry(monkeypatch, captured)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "p")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)

    backend_memex.write_task(
        title="t",
        description="",
        project_id=1,
        created_by="atelier-engineer-1",
    )
    # The atelier#30 filter sees workspace_id in metadata even though
    # the caller didn't provide one — the injection in _atelier_write
    # is what activates the multi-workspace guard.
    assert metadata_seen.get("workspace_id") == 11


def test_atelier_write_preserves_caller_supplied_workspace_id(monkeypatch):
    """When the caller passes `workspace_id=N` in metadata, the
    injection step MUST NOT overwrite it with the singleton's id —
    the caller's choice wins (matches the `setdefault` semantics
    already used for other metadata keys)."""
    monkeypatch.setattr(
        backend_memex,
        "_workspace_slug_for_id",
        lambda workspace_id: "found",
    )
    metadata_seen: dict = {}

    def spy_auto_relations(metadata, explicit):
        metadata_seen.update(metadata)
        return list(explicit or [])

    monkeypatch.setattr(backend_memex, "_auto_relations", spy_auto_relations)
    captured: dict = {}
    _stub_write_entry(monkeypatch, captured)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "p")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)

    backend_memex.write_document(
        domain="design",
        title="x",
        body="b",
        metadata={"workspace_id": 99, "project_id": 1},
        caller_agent_id="atelier-engineer-1",
    )
    # Caller's 99 wins; the singleton fallback wasn't invoked.
    assert metadata_seen.get("workspace_id") == 99


# ── _resolve_singleton_workspace_id no longer slug-filters ────────────────


def test_resolve_singleton_workspace_id_uses_first_row(monkeypatch):
    """Post-#55, `_resolve_singleton_workspace_id` delegates to
    `_singleton_workspace` — no `where={"slug": ...}` filter survives."""
    monkeypatch.setattr(
        backend_memex,
        "_singleton_workspace",
        lambda: {"id": 17, "slug": "anything"},
    )
    assert backend_memex._resolve_singleton_workspace_id() == 17


# ── helper ────────────────────────────────────────────────────────────────


def _stub_write_entry(monkeypatch, captured: dict) -> None:
    """Stub the Memex write+validate+embed paths so the test runs
    hermetically; capture the librarian_output for assertion."""
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": "x",
                "key": k["librarian_output"]["key"],
                "domain": k["librarian_output"]["domain"],
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
