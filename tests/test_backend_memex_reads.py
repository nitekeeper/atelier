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
import json as _json
import sqlite3
import sys
import types
from unittest.mock import patch

from scripts import backend_memex


def _patch_memex_module(monkeypatch, dotted: str, replacement) -> None:
    """Inject `replacement` as the Memex module that production resolves
    via `_load_memex_module(plugin_root, <dotted>)`.

    The C1 refactor replaced `from scripts.<name> import ...` with an
    importlib-based file loader (sidestepping the `scripts.agents`
    namespace collision), so the older sys.modules-patch trick is no
    longer effective. The simplest test-time hook is to swap
    `_memex_module` for a closure that maps known dotted names to the
    test's fake objects and falls back to the real loader otherwise.

    Detects whether the current `_memex_module` is already a fake from
    a prior call within this test (it then carries a `_fakes` mapping)
    and just extends that mapping; otherwise installs a fresh fake.
    """
    current = backend_memex._memex_module
    fakes = getattr(current, "_fakes", None)
    if fakes is None:
        fakes = {}
        real_loader = current

        def fake_memex_module(name: str):
            if name in fakes:
                return fakes[name]
            return real_loader(name)

        fake_memex_module._fakes = fakes  # type: ignore[attr-defined]
        monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)
    fakes[dotted] = replacement


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
    # Default filters: no project_id, no domain, default limit. Nit N12:
    # the query string must reach _memex_search untouched.
    mock_search.assert_called_once()
    assert mock_search.call_args.kwargs["query"] == "auth design"


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


def test_memex_search_post_filters_project_id(monkeypatch):
    """I1: Memex's `reference_librarian.execute_query_plan` only honors
    `domain` / `store` filters — `project_id` is dropped silently. The
    `_memex_search` wrapper must therefore post-filter on
    `metadata.project_id` after the Memex call returns."""
    # 4 candidate rows; only #2 and #3 belong to project_id=42.
    fake_rows = [
        {"index_id": "01", "key": "k-a", "metadata": {"project_id": 1}},
        {"index_id": "02", "key": "k-b", "metadata": {"project_id": 42}},
        {"index_id": "03", "key": "k-c", "metadata": {"project_id": 42}},
        {"index_id": "04", "key": "k-d", "metadata": {"project_id": 7}},
    ]
    captured_plan = {}

    fake_ref = types.SimpleNamespace(
        execute_query_plan=lambda plan, with_embedding: (
            captured_plan.update(plan), list(fake_rows))[1]
    )
    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "agents.reference_librarian":
            return fake_ref
        return real_memex_module(name)
    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    results = backend_memex._memex_search(
        query="x", project_id=42, limit=10)
    # Only the project_id=42 rows survive the post-filter.
    keys = [r["key"] for r in results]
    assert keys == ["k-b", "k-c"]
    # Sanity: the native plan filter dict did NOT carry project_id
    # (reference_librarian wouldn't know what to do with it).
    assert "project_id" not in captured_plan.get("filters", {})


def test_memex_search_post_filter_decodes_json_metadata(monkeypatch):
    """The `metadata` column comes back as a JSON STRING in
    real-world Memex reads (`scripts.stores` serializes on insert and
    leaves the column unparsed in many code paths). The post-filter
    must JSON-decode it before reaching for `.get("project_id")`."""
    fake_rows = [
        {"index_id": "01", "metadata": _json.dumps({"project_id": 42})},
        {"index_id": "02", "metadata": _json.dumps({"project_id": 99})},
    ]
    fake_ref = types.SimpleNamespace(
        execute_query_plan=lambda plan, with_embedding: list(fake_rows)
    )
    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "agents.reference_librarian":
            return fake_ref
        return real_memex_module(name)
    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    results = backend_memex._memex_search(
        query="x", project_id=42, limit=10)
    assert [r["index_id"] for r in results] == ["01"]


def test_get_task_returns_row():
    """task_id present in atelier.db.tasks → returns the row dict."""
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[{"id": 1, "title": "Fix bug"}]) as q:
        r = backend_memex.get_task(task_id=1)
    assert r["title"] == "Fix bug"
    # Nit N13: the underlying query must filter on id, not blindly
    # return the first row of the tasks table.
    assert q.call_args.kwargs["where"] == {"id": 1}
    assert q.call_args.kwargs["table"] == "tasks"


def test_get_task_missing_returns_none():
    """task_id absent → returns None (NOT KeyError)."""
    with patch.object(backend_memex, "_memex_core_query", return_value=[]):
        assert backend_memex.get_task(task_id=999) is None


def test_list_tasks_filters_by_project():
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[{"id": 1}, {"id": 2}]) as q:
        backend_memex.list_tasks(project_id=1)
    assert q.call_args.kwargs["where"]["project_id"] == 1
    # Nit N14: without an explicit `status` argument, the WHERE must
    # NOT carry a status filter (so we get all rows for the project).
    assert "status" not in q.call_args.kwargs["where"]


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
    _patch_memex_module(monkeypatch, "stores",
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
    _patch_memex_module(monkeypatch, "stores",
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

    _patch_memex_module(monkeypatch, "roles",
                             types.SimpleNamespace(list_roles=list_roles,
                                                   create_role=create_role))

    r = backend_memex.find_or_create_role(name="Product Manager",
                                           description="PM coord")
    assert r["id"] == 7
    assert created["name"] == "Product Manager"


def test_find_or_create_role_idempotent_on_second_call(monkeypatch):
    """Two consecutive calls with the same name return the SAME id and
    never trigger a second `create_role` (Nit N8: must exercise TWO
    calls, not one, to validate the idempotence claim)."""
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")

    create_calls: list = []
    state: list[dict] = []

    def list_roles(db_path):
        return list(state)

    def create_role(db_path, *, name, description):
        create_calls.append((name, description))
        row = {"id": 3, "name": name, "description": description}
        state.append(row)
        return row

    _patch_memex_module(monkeypatch, "roles",
                             types.SimpleNamespace(list_roles=list_roles,
                                                   create_role=create_role))

    # First call: misses, creates row id=3.
    r1 = backend_memex.find_or_create_role(name="Product Manager",
                                            description="PM coord")
    assert r1["id"] == 3
    assert len(create_calls) == 1
    # Second call with the same name: hits the populated list, returns
    # the EXISTING row, MUST NOT invoke create_role a second time.
    r2 = backend_memex.find_or_create_role(name="Product Manager",
                                            description="ignored on second call")
    assert r2["id"] == 3
    assert r2 is r1 or r2["name"] == r1["name"]
    assert len(create_calls) == 1, "create_role must not be re-invoked"


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

    _patch_memex_module(monkeypatch, "agents",
                             types.SimpleNamespace(get_agent=get_agent,
                                                   create_agent=create_agent))

    r = backend_memex.find_or_create_agent(
        agent_id="atelier-pm-1", name="PM", role_id=7, profile="pm")
    assert r["id"] == "atelier-pm-1"
    assert created["role_id"] == 7


def test_find_or_create_agent_idempotent_on_second_call(monkeypatch):
    """Two consecutive calls with the same agent_id return the existing
    row; `create_agent` must fire AT MOST once (on the first call when
    the agents DB is empty) and never on the second call (Nit N8)."""
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    create_calls: list = []
    state: dict[str, dict] = {}

    def get_agent(db_path, agent_id):
        return state.get(agent_id)

    def create_agent(db_path, agent_id, name, role_id, profile):
        create_calls.append((agent_id, name, role_id, profile))
        row = {"id": agent_id, "name": name, "role_id": role_id,
               "profile": profile}
        state[agent_id] = row
        return row

    _patch_memex_module(monkeypatch, "agents",
                             types.SimpleNamespace(get_agent=get_agent,
                                                   create_agent=create_agent))

    # First call: misses, creates.
    r1 = backend_memex.find_or_create_agent(
        agent_id="atelier-pm-1", name="PM", role_id=3, profile="pm")
    assert r1["id"] == "atelier-pm-1"
    assert len(create_calls) == 1
    # Second call with the same agent_id: returns existing, no create.
    r2 = backend_memex.find_or_create_agent(
        agent_id="atelier-pm-1", name="ignored", role_id=99,
        profile="ignored")
    assert r2["id"] == "atelier-pm-1"
    assert r2["name"] == "PM", "must return existing row, not replace"
    assert len(create_calls) == 1, "create_agent must not be re-invoked"


def test_memex_core_execute_returns_rowcount(monkeypatch, tmp_path):
    """Happy path — runs raw SQL against the resolved store path,
    commits, returns affected rowcount, AND the commit survives across
    fresh connections (I7: the rowcount assertion alone could pass even
    if `conn.commit()` were dropped, since the same connection sees
    its own uncommitted writes)."""
    db = tmp_path / "fake.db"
    # Seed a tiny table
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
    conn.execute("INSERT INTO t VALUES (1, 10), (1, 20), (2, 30)")
    conn.commit()
    initial = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert initial == 3

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    fake_registry = types.SimpleNamespace(
        get_store=lambda name: ({"path": str(db)}
                                if name == "atelier" else None)
    )
    _patch_memex_module(monkeypatch, "registry", fake_registry)
    # Memex's get_connection sets pragmas; we stub with stdlib sqlite3
    # since the fake.db doesn't need them.
    fake_db = types.SimpleNamespace(
        get_connection=lambda p: sqlite3.connect(p)
    )
    _patch_memex_module(monkeypatch, "db", fake_db)

    n = backend_memex._memex_core_execute(
        store="atelier", sql="DELETE FROM t WHERE a = ?", params=(1,))
    assert n == 2
    # I7: reopen the DB on a fresh connection and confirm the DELETE
    # was committed (otherwise the rowcount could be a phantom).
    con2 = sqlite3.connect(db)
    remaining = con2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    con2.close()
    assert remaining == initial - 2


def test_memex_core_execute_no_match_returns_zero(monkeypatch, tmp_path):
    """N7: a DELETE that matches nothing returns rowcount=0 (NOT raise).
    The store path is resolved and the SQL is dispatched normally — we
    just expect zero rows affected when the WHERE predicate is false."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    fake_registry = types.SimpleNamespace(
        get_store=lambda name: ({"path": str(db)}
                                if name == "atelier" else None)
    )
    _patch_memex_module(monkeypatch, "registry", fake_registry)
    fake_db = types.SimpleNamespace(
        get_connection=lambda p: sqlite3.connect(p)
    )
    _patch_memex_module(monkeypatch, "db", fake_db)

    n = backend_memex._memex_core_execute(
        store="atelier", sql="DELETE FROM t WHERE a = ?", params=(999,))
    assert n == 0
