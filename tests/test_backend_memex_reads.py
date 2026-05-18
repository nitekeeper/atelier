# tests/test_backend_memex_reads.py
"""Tests for Plan 2 Task 3 — Memex-mode reads + cross-plan helpers.

find_documents dispatches FTS5-only search via Memex's reference
librarian; get_task / list_tasks read through Core CRUD.

Cross-plan helpers:
- lookup_index_id_by_source_ref: idempotent migrator round-trip lookup.
- _agents_db_path / find_or_create_role / find_or_create_agent:
  idempotent role/agent management against ~/.memex/agents.db.
- _memex_core_execute: composite-key DELETE primitive (used by Plan 3
  meeting_participants rewrite).
"""
import sys
import sqlite3
import types
from unittest.mock import patch

from scripts import backend_memex


def _patch_scripts_submodule(monkeypatch, name: str, replacement) -> None:
    """Replace `scripts.<name>` with `replacement` for the test scope.

    Python's `from scripts import <name>` resolves to the parent
    package's attribute (set on first import) rather than re-consulting
    `sys.modules['scripts.<name>']`, so patching ONLY sys.modules is
    insufficient when Atelier has already imported its own submodule
    by that name (the parent package then has a `name` attribute
    pointing at Atelier's real module). We patch both: sys.modules
    (for fresh imports) AND the parent attribute (for already-imported
    submodules).
    """
    import scripts as scripts_pkg  # noqa: PLC0415
    monkeypatch.setitem(sys.modules, f"scripts.{name}", replacement)
    monkeypatch.setattr(scripts_pkg, name, replacement, raising=False)


# ── find_documents / get_task / list_tasks ──────────────────────────────


def test_find_documents_dispatches_to_memex_search():
    """The public method just forwards to _memex_search — the meaningful
    routing logic (FTS5 filter assembly) lives there."""
    fake_results = [
        {"index_id": "01a", "key": "design-auth", "domain": "design",
         "store": "atelier", "row_id": 1, "searchable": "auth design"},
    ]
    with patch.object(backend_memex, "_memex_search",
                      return_value=fake_results) as mock_search:
        results = backend_memex.find_documents(query="auth design")
    assert len(results) == 1
    assert results[0]["key"] == "design-auth"
    # Default filters: no project_id, no domain, default limit.
    mock_search.assert_called_once()


def test_find_documents_passes_domain_filter():
    """A domain filter must flow through to _memex_search."""
    with patch.object(backend_memex, "_memex_search",
                      return_value=[]) as mock_search:
        backend_memex.find_documents(query="x", domain="adr")
    assert mock_search.call_args.kwargs["domain"] == "adr"


def test_find_documents_passes_project_filter():
    """A project_id filter must flow through to _memex_search."""
    with patch.object(backend_memex, "_memex_search",
                      return_value=[]) as mock_search:
        backend_memex.find_documents(query="x", project_id=42)
    assert mock_search.call_args.kwargs["project_id"] == 42


def test_find_documents_passes_limit():
    """The limit cap must flow through to _memex_search."""
    with patch.object(backend_memex, "_memex_search",
                      return_value=[]) as mock_search:
        backend_memex.find_documents(query="x", limit=3)
    assert mock_search.call_args.kwargs["limit"] == 3


def test_get_task_returns_row():
    """task_id present in atelier.db.tasks → returns the row dict."""
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[{"id": 1, "title": "Fix bug"}]):
        r = backend_memex.get_task(task_id=1)
    assert r["title"] == "Fix bug"


def test_get_task_missing_returns_none():
    """task_id absent → returns None (NOT KeyError)."""
    with patch.object(backend_memex, "_memex_core_query", return_value=[]):
        assert backend_memex.get_task(task_id=999) is None


def test_list_tasks_filters_by_project():
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[{"id": 1}, {"id": 2}]) as q:
        backend_memex.list_tasks(project_id=1)
    assert q.call_args.kwargs["where"]["project_id"] == 1


def test_list_tasks_can_filter_by_status():
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[]) as q:
        backend_memex.list_tasks(project_id=1, status="blocked")
    assert q.call_args.kwargs["where"]["status"] == "blocked"


# ── Cross-plan helpers ─────────────────────────────────────────────────


def test_lookup_index_id_by_source_ref_returns_id(monkeypatch):
    """Source-ref present in the Index — returns the index_id."""
    captured = {}

    def fake_query(name, sql, params):
        captured["name"] = name
        captured["sql"] = sql
        captured["params"] = params
        return [{"index_id": "01HXYZ-task-42"}]

    # backend_memex imports stores lazily; patch the module attribute
    # _ensure_memex_importable resolves to.
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    _patch_scripts_submodule(monkeypatch, "stores",
                             types.SimpleNamespace(query=fake_query))

    result = backend_memex.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:42")
    assert result == "01HXYZ-task-42"
    # Must target the federated index, not atelier
    assert captured["name"] == "index"
    assert "json_extract(metadata, '$.source_ref')" in captured["sql"]
    assert captured["params"] == ("atelier:tasks:42",)


def test_lookup_index_id_by_source_ref_returns_none_when_absent(monkeypatch):
    """Source-ref absent — returns None (NOT KeyError, NOT empty string)."""
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    _patch_scripts_submodule(monkeypatch, "stores",
                             types.SimpleNamespace(query=lambda *a, **k: []))
    assert backend_memex.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:999") is None


def test_find_or_create_role_creates_on_miss(monkeypatch):
    """Role absent — creates and returns the new row."""
    listed: list[dict] = []
    created: dict = {}

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")

    def list_roles(db_path):
        return list(listed)

    def create_role(db_path, *, name, description):
        created["name"] = name
        created["description"] = description
        row = {"id": 7, "name": name, "description": description}
        listed.append(row)
        return row

    _patch_scripts_submodule(monkeypatch, "roles",
                             types.SimpleNamespace(list_roles=list_roles,
                                                   create_role=create_role))

    r = backend_memex.find_or_create_role(name="Product Manager",
                                           description="PM coord")
    assert r["id"] == 7
    assert created["name"] == "Product Manager"


def test_find_or_create_role_idempotent_on_second_call(monkeypatch):
    """Second call with same name returns the SAME id (idempotent)."""
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")

    create_calls: list = []

    def list_roles(db_path):
        # First call seeds; second call now hits the populated list.
        return [{"id": 3, "name": "Product Manager",
                 "description": "existing"}]

    def create_role(db_path, *, name, description):
        create_calls.append((name, description))
        return {}

    _patch_scripts_submodule(monkeypatch, "roles",
                             types.SimpleNamespace(list_roles=list_roles,
                                                   create_role=create_role))

    r = backend_memex.find_or_create_role(name="Product Manager",
                                           description="ignored")
    assert r["id"] == 3
    assert create_calls == []


def test_find_or_create_agent_creates_on_miss(monkeypatch):
    """Agent absent — creates and returns the new row."""
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    created: dict = {}

    def get_agent(db_path, agent_id):
        return None

    def create_agent(db_path, agent_id, name, role_id, profile):
        created.update(agent_id=agent_id, name=name,
                       role_id=role_id, profile=profile)
        return {"id": agent_id, "name": name, "role_id": role_id,
                "profile": profile}

    _patch_scripts_submodule(monkeypatch, "agents",
                             types.SimpleNamespace(get_agent=get_agent,
                                                   create_agent=create_agent))

    r = backend_memex.find_or_create_agent(
        agent_id="atelier-pm-1", name="PM", role_id=7, profile="pm")
    assert r["id"] == "atelier-pm-1"
    assert created["role_id"] == 7


def test_find_or_create_agent_idempotent_on_second_call(monkeypatch):
    """Second call with same agent_id returns the existing row, no create."""
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    create_calls: list = []

    def get_agent(db_path, agent_id):
        return {"id": agent_id, "name": "PM", "role_id": 3}

    def create_agent(*a, **k):
        create_calls.append((a, k))
        return {}

    _patch_scripts_submodule(monkeypatch, "agents",
                             types.SimpleNamespace(get_agent=get_agent,
                                                   create_agent=create_agent))

    r = backend_memex.find_or_create_agent(
        agent_id="atelier-pm-1", name="ignored", role_id=99,
        profile="ignored")
    assert r["name"] == "PM"
    assert create_calls == []


def test_memex_core_execute_returns_rowcount(monkeypatch, tmp_path):
    """Happy path — runs raw SQL against the resolved store path,
    commits, returns affected rowcount."""
    db = tmp_path / "fake.db"
    # Seed a tiny table
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
    conn.execute("INSERT INTO t VALUES (1, 10), (1, 20), (2, 30)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    fake_registry = types.SimpleNamespace(
        get_store=lambda name: ({"path": str(db)}
                                if name == "atelier" else None)
    )
    _patch_scripts_submodule(monkeypatch, "registry", fake_registry)
    # Memex's get_connection sets pragmas; we stub with stdlib sqlite3
    # since the fake.db doesn't need them.
    fake_db = types.SimpleNamespace(
        get_connection=lambda p: sqlite3.connect(p)
    )
    _patch_scripts_submodule(monkeypatch, "db", fake_db)

    n = backend_memex._memex_core_execute(
        store="atelier", sql="DELETE FROM t WHERE a = ?", params=(1,))
    assert n == 2
