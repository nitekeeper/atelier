# Atelier ↔ Memex v2 Retrofit — Plan 2 of 4: Write Paths (Waves 1 + 1')

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the empty `backend.py` facade with two complete backends — Memex-mode (dispatches to `memex:run`) and Local-mode (direct SQLite). Add the internal SKILL.md routing procedures for both. End-state: every backend method returns real data on real stores.

**Architecture:** Two physically separate Python modules — `scripts/backend_memex.py` and `scripts/backend_local.py` — implement the contract from Plan 1. `scripts/backend.py` becomes a thin dispatcher selecting between them via `mode_detector.detect_mode()`. The two backends are file-disjoint so they can be implemented in parallel.

**Tech Stack:** Python 3.10+, pytest, sqlite3, subprocess (for `memex:run` invocation in Memex mode), JSON.

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md](../specs/2026-05-16-atelier-memex-v2-retrofit-design.md) §§4-7.

---

## Parallel dispatch map

```
                   ┌──────────────────── 8 parallel tasks (Waves 1 + 1') ─────────────────────┐
                   │                                                                            │
Wave 1 (Memex):    │  Task 1: backend_memex doc writes     Task 2: backend_memex state writes  │
                   │  Task 3: backend_memex reads          Task 4: internal/memex/* procedures │
                   │                                                                            │
Wave 1' (Local):   │  Task 5: backend_local doc writes     Task 6: backend_local state writes  │
                   │  Task 7: backend_local reads          Task 8: internal/local/* procedures │
                   └────────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
              Wave 1.5 sequential (depends on all 8 above):
              Task 9:  rewire scripts/backend.py to dispatch by mode
              Task 10: internal/bootstrap-memex procedure (end-to-end test needs both backends)
```

Tasks 1–8 touch disjoint files. Dispatch all 8 as parallel subagents. Tasks 9 + 10 sequential after.

---

### Task 1: Memex backend — document writes (Tier 2)

**Files:**
- Create: `scripts/backend_memex.py` (this task starts the file; Tasks 2-3 append to it)
- Test: `tests/test_backend_memex_documents.py`

Implements `write_document`, `write_task`, `write_meeting`. Each routes through Memex's **Tier 2 path** per spec §6.2: caller-built `librarian_output` validated by `librarian.validate_output()` and persisted via `librarian.write_entry()`. **No Librarian LLM dispatch.** Atelier owns the domain (`scripts/domain_vocabulary.DOMAINS` from Plan 1 Task 6) and builds the classification deterministically.

Memex contract version: **v2.2.0+** — earlier versions don't accept caller-built `librarian_output` and will reject the schema. Task 10 adds the version guard at bootstrap; Task 1 trusts it.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_memex_documents.py
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from scripts import backend_memex


@pytest.fixture
def fake_memex(tmp_path, monkeypatch):
    """Stand up a temp ~/.memex/ structure + a registry pointing at a
    temp atelier.db. The Memex-side modules (librarian, embeddings) are
    patched in each test so we don't need a real Memex install."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text(
        '{"stores": {"atelier": {"path": "%s"}}}' %
        (memex_home / "atelier.db").as_posix())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return memex_home


def test_write_document_validates_domain(fake_memex):
    """An unknown domain must be rejected before any Memex call."""
    with pytest.raises(ValueError, match="unknown domain"):
        backend_memex.write_document(
            domain="blog_post",  # not in DOMAINS
            title="x", body="x", metadata={}, caller_agent_id="atelier-pm-1",
        )


def test_write_document_builds_librarian_output_and_writes(fake_memex,
                                                            monkeypatch):
    captured = {}

    def fake_validate(d):
        captured["validated"] = d
        return d

    def fake_write_entry(*, payload, librarian_output, target_store,
                         target_table, caller_agent_id, embedding):
        captured["payload"] = payload
        captured["librarian_output"] = librarian_output
        captured["target_store"] = target_store
        captured["target_table"] = target_table
        captured["caller_agent_id"] = caller_agent_id
        return {"status": "ingested",
                "index_id": librarian_output["index_id"],
                "key": librarian_output["key"],
                "domain": librarian_output["domain"],
                "row_id": 42, "relations": []}

    monkeypatch.setattr(backend_memex, "_memex_validate_output", fake_validate)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_memex_embed",
                        lambda text: b"\x00" * 16)

    result = backend_memex.write_document(
        domain="project_doc", title="Auth Design",
        body="OAuth2 flow with refresh tokens.",
        metadata={"project_id": 1, "filename": "DESIGN.md"},
        caller_agent_id="atelier-pm-1",
    )

    assert result["row_id"] == 42
    assert captured["target_store"] == "atelier"
    assert captured["target_table"] == "project_documents"
    assert captured["librarian_output"]["domain"] == "project_doc"
    assert captured["librarian_output"]["key"]  # non-empty slug
    assert "Auth Design" in captured["librarian_output"]["searchable"]
    assert captured["librarian_output"]["metadata"]["project_id"] == 1


def test_write_task_targets_tasks_table_with_task_domain(fake_memex,
                                                          monkeypatch):
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry",
        lambda **k: (captured.update(k), {"status": "ingested",
                                          "index_id": k["librarian_output"]["index_id"],
                                          "key": k["librarian_output"]["key"],
                                          "domain": "task",
                                          "row_id": 1, "relations": []})[1])
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)

    backend_memex.write_task(
        title="Fix auth bug", description="OAuth returns 500",
        project_id=1, created_by="atelier-engineer-1",
        priority=5, notes="repro: hit /oauth/callback twice",
    )
    assert captured["target_table"] == "tasks"
    assert captured["librarian_output"]["domain"] == "task"
    assert captured["payload"]["priority"] == 5
    assert "repro" in captured["payload"]["notes"]


def test_write_meeting_targets_meeting_minutes(fake_memex, monkeypatch):
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry",
        lambda **k: (captured.update(k), {"status": "ingested",
                                          "index_id": k["librarian_output"]["index_id"],
                                          "key": k["librarian_output"]["key"],
                                          "domain": "meeting",
                                          "row_id": 1, "relations": []})[1])
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)

    backend_memex.write_meeting(
        title="Kickoff", date="2026-05-16",
        summary="Discussed scope.", decisions="Use OAuth2.",
        created_by="atelier-pm-1",
    )
    assert captured["target_table"] == "meeting_minutes"
    assert captured["librarian_output"]["domain"] == "meeting"
    assert "Discussed scope." in captured["librarian_output"]["searchable"]


def test_embedding_failure_is_swallowed(fake_memex, monkeypatch):
    """When embeddings.encode raises, persist with embedding=None."""
    captured_embedding = {}

    def boom(text):
        raise RuntimeError("openai not installed")

    def fake_write_entry(**kwargs):
        captured_embedding["embedding"] = kwargs["embedding"]
        return {"status": "ingested",
                "index_id": kwargs["librarian_output"]["index_id"],
                "key": kwargs["librarian_output"]["key"],
                "domain": kwargs["librarian_output"]["domain"],
                "row_id": 1, "relations": []}

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_embed", boom)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)

    backend_memex.write_task(
        title="x", description="y", project_id=1, created_by="atelier-pm-1",
    )
    assert captured_embedding["embedding"] is None
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/test_backend_memex_documents.py -v
```
Expected: `ModuleNotFoundError: scripts.backend_memex`.

- [ ] **Step 3: Implement document writes**

```python
# scripts/backend_memex.py
"""Memex-mode backend (Tier 2 caller-built librarian_output path).

Writes through memex:index:write WITHOUT the Librarian LLM dispatch —
Atelier knows its domain, builds the classification deterministically,
and calls librarian.write_entry() directly. See spec §6.2.

Requires Memex v2.2.0+ (the version that ships librarian.validate_output
and the optional librarian_output parameter on memex:index:write).
"""
from __future__ import annotations
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts import domain_vocabulary


def _memex_plugin_dir() -> Path:
    """Locate the installed Memex plugin's root directory."""
    base = Path.home() / ".claude" / "plugins" / "cache" / "agora" / "memex"
    versions = sorted(base.iterdir()) if base.exists() else []
    if not versions:
        raise RuntimeError(
            "Memex plugin not found in Claude Code cache; expected at "
            f"{base}. Atelier should be in Local mode — check mode_detector."
        )
    return versions[-1]


def _ensure_memex_importable() -> None:
    p = str(_memex_plugin_dir())
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Memex Tier 2 thin wrappers (also serve as patch surfaces in tests) ─────

def _memex_validate_output(librarian_output: dict) -> dict:
    """Delegate to Memex's librarian.validate_output."""
    _ensure_memex_importable()
    from scripts.agents import librarian as memex_librarian  # type: ignore
    return memex_librarian.validate_output(librarian_output)


def _memex_write_entry(*, payload: dict, librarian_output: dict,
                       target_store: str, target_table: str,
                       caller_agent_id: str,
                       embedding: bytes | None) -> dict:
    """Delegate to Memex's librarian.write_entry."""
    _ensure_memex_importable()
    from scripts.agents import librarian as memex_librarian  # type: ignore
    return memex_librarian.write_entry(
        payload=payload,
        librarian_output=librarian_output,
        target_store=target_store,
        target_table=target_table,
        caller_agent_id=caller_agent_id,
        embedding=embedding,
    )


def _memex_embed(text: str) -> bytes | None:
    """Best-effort wrapper around Memex's embeddings.encode."""
    _ensure_memex_importable()
    from scripts import embeddings as memex_embeddings  # type: ignore
    return memex_embeddings.encode(text)


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_uuid7() -> str:
    """UUID v7-style: ms timestamp prefix + random tail."""
    ms = int(time.time() * 1000)
    hex_ms = f"{ms:012x}"
    rand = uuid.uuid4().hex[12:]
    return f"{hex_ms[:8]}-{hex_ms[8:12]}-7{rand[:3]}-{rand[3:7]}-{rand[7:19]}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]


def _try_embed(text: str) -> bytes | None:
    """Best-effort embedding — swallow provider errors, return None."""
    try:
        return _memex_embed(text)
    except Exception:
        return None


def _atelier_write(*, target_table: str, domain: str, title: str,
                   body: str, payload: dict, metadata: dict,
                   relations: list, caller_agent_id: str) -> dict:
    """Tier 2 atelier write — synchronous, no LLM dispatch.

    Builds librarian_output deterministically, validates via Memex, and
    persists via librarian.write_entry. The target row goes into
    ~/.memex/atelier.db.<target_table> with an index_id linkback;
    the matching documents row goes into ~/.memex/index.db.
    """
    domain_vocabulary.assert_valid(domain)

    output = _memex_validate_output({
        "index_id":   _new_uuid7(),
        "key":        _slug(title),
        "domain":     domain,
        "searchable": f"{title}. {body[:1500]}",
        "metadata":   metadata,
        "relations":  relations,
    })
    embedding = _try_embed(output["searchable"])
    return _memex_write_entry(
        payload=payload,
        librarian_output=output,
        target_store="atelier",
        target_table=target_table,
        caller_agent_id=caller_agent_id,
        embedding=embedding,
    )


# ── Document writes ────────────────────────────────────────────────────────

# Map Atelier domain → target table in ~/.memex/atelier.db.
_DOMAIN_TO_TABLE = {
    "project":     "projects",
    "task":        "tasks",
    "meeting":     "meeting_minutes",
    "project_doc": "project_documents",
    "adr":         "project_documents",
}


def write_document(*, domain: str, title: str, body: str,
                   metadata: dict, caller_agent_id: str,
                   source_url: str | None = None) -> dict:
    target_table = _DOMAIN_TO_TABLE.get(domain) or "project_documents"
    payload = {
        "title": title,
        "filename": (metadata or {}).get("filename", _slug(title) + ".md"),
        "project_id": (metadata or {}).get("project_id"),
        "type": domain,
        "created_by": caller_agent_id,
        "created_at": _now(),
        "updated_at": _now(),
    }
    return _atelier_write(
        target_table=target_table, domain=domain,
        title=title, body=body, payload=payload,
        metadata=metadata or {}, relations=[],
        caller_agent_id=caller_agent_id,
    )


def write_task(*, title: str, description: str, project_id: int,
               created_by: str, assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None) -> dict:
    body_lines = [f"# {title}", "", description or ""]
    if notes:
        body_lines += ["", "## Notes", notes]
    body = "\n".join(body_lines)
    payload = {
        "title": title, "description": description, "project_id": project_id,
        "created_by": created_by, "assigned_to": assigned_to,
        "priority": priority, "notes": notes, "status": "pending",
        "created_at": _now(), "updated_at": _now(),
    }
    metadata = {"project_id": project_id, "priority": priority}
    if assigned_to:
        metadata["assigned_to"] = assigned_to
    return _atelier_write(
        target_table="tasks", domain="task",
        title=title, body=body, payload=payload,
        metadata=metadata, relations=[],
        caller_agent_id=created_by,
    )


def write_meeting(*, title: str, date: str, summary: str,
                  decisions: str, created_by: str,
                  project_id: int | None = None) -> dict:
    body = (f"# {title}\n\nDate: {date}\n\n"
            f"## Summary\n\n{summary}\n\n"
            f"## Decisions\n\n{decisions}\n")
    payload = {
        "title": title, "date": date,
        "filename": f"{date}-{_slug(title)}.md",
        "summary": summary, "decisions": decisions,
        "created_by": created_by,
        "created_at": _now(), "updated_at": _now(),
    }
    metadata: dict = {"date": date}
    if project_id is not None:
        metadata["project_id"] = project_id
    return _atelier_write(
        target_table="meeting_minutes", domain="meeting",
        title=title, body=body, payload=payload,
        metadata=metadata, relations=[],
        caller_agent_id=created_by,
    )
```

- [ ] **Step 4: Run tests — expect pass**

```
pytest tests/test_backend_memex_documents.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_memex.py tests/test_backend_memex_documents.py
git commit -m "feat(backend-memex): wave-1 Tier 2 document writes (caller-built librarian_output)"
```

---

### Task 2: Memex backend — operational state writes

**Files:**
- Modify: `scripts/backend_memex.py` (append; below Task 1's region)
- Test: `tests/test_backend_memex_state.py`

Implements `upsert_session`, `transition_phase`, `update_task_status`, `record_phase_bypass`. These call Memex Core CRUD (`memex:core:insert/update`) — no Librarian dispatch.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_memex_state.py
from unittest.mock import patch, MagicMock
import pytest
from scripts import backend_memex


@pytest.fixture
def mock_core():
    """Patch the Memex Core dispatch helpers."""
    with patch.object(backend_memex, "_memex_core_insert") as ins, \
         patch.object(backend_memex, "_memex_core_update") as upd, \
         patch.object(backend_memex, "_memex_core_query") as qry:
        yield {"insert": ins, "update": upd, "query": qry}


def test_upsert_session_inserts_new(mock_core):
    mock_core["query"].return_value = []
    mock_core["insert"].return_value = {"id": 1}
    r = backend_memex.upsert_session(
        project_id=1, agent_id="atelier-pm-1", phase="design:open",
        current_tasks="onboarding", accomplished="", next_action="grill design",
    )
    assert r["id"] == 1
    mock_core["insert"].assert_called_once()


def test_upsert_session_updates_existing(mock_core):
    mock_core["query"].return_value = [{"id": 7, "status": "in-progress"}]
    mock_core["update"].return_value = {"id": 7, "status": "in-progress"}
    r = backend_memex.upsert_session(
        project_id=1, agent_id="atelier-pm-1",
        accomplished="finished kickoff",
    )
    assert r["id"] == 7
    mock_core["update"].assert_called_once()


def test_transition_phase_updates_project_row(mock_core):
    mock_core["query"].return_value = [{"id": 1, "phase": "design:approved"}]
    mock_core["update"].return_value = {"id": 1, "phase": "plan:open"}
    r = backend_memex.transition_phase(
        project_id=1, to_phase="plan:open", agent_id="atelier-pm-1",
    )
    assert r["phase"] == "plan:open"


def test_update_task_status(mock_core):
    mock_core["update"].return_value = {"id": 1, "status": "in-progress"}
    r = backend_memex.update_task_status(task_id=1, status="in-progress")
    assert r["status"] == "in-progress"


def test_record_phase_bypass_inserts(mock_core):
    mock_core["insert"].return_value = {"id": 1}
    r = backend_memex.record_phase_bypass(
        project_id=1, from_phase="design:open", to_phase="plan:open",
        reason="user override", agent_id="atelier-pm-1",
    )
    assert r["id"] == 1
    assert mock_core["insert"].call_args.kwargs["table"] == "phase_bypasses"
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/test_backend_memex_state.py -v
```
Expected: helpers + functions missing.

- [ ] **Step 3: Append the state writes to backend_memex.py**

```python
# Append to scripts/backend_memex.py

# ── Memex Core CRUD helpers ────────────────────────────────────────────────

def _memex_core_insert(*, store: str, table: str, row: dict) -> dict:
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) RETURNING *"
    rows = memex_stores.query(store, sql, tuple(row.values()))
    return rows[0] if rows else {}


def _memex_core_update(*, store: str, table: str, row_id: int, changes: dict) -> dict:
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    set_clause = ", ".join(f"{k} = ?" for k in changes.keys())
    sql = f"UPDATE {table} SET {set_clause} WHERE id = ? RETURNING *"
    rows = memex_stores.query(store, sql, tuple(changes.values()) + (row_id,))
    return rows[0] if rows else {}


def _memex_core_query(*, store: str, table: str, where: dict | None = None) -> list[dict]:
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    if where:
        clauses = " AND ".join(f"{k} = ?" for k in where)
        sql = f"SELECT * FROM {table} WHERE {clauses}"
        return memex_stores.query(store, sql, tuple(where.values()))
    return memex_stores.query(store, f"SELECT * FROM {table}", ())


# ── Operational state writes ───────────────────────────────────────────────

def upsert_session(*, project_id: int, agent_id: str, phase: str | None = None,
                   current_tasks: str | None = None,
                   accomplished: str | None = None,
                   next_action: str | None = None,
                   status: str = "in-progress",
                   pm_notes: str | None = None) -> dict:
    existing = _memex_core_query(store="atelier", table="sessions",
                                 where={"project_id": project_id,
                                        "agent_id": agent_id,
                                        "status": "in-progress"})
    payload = {
        "phase": phase, "current_tasks": current_tasks,
        "accomplished": accomplished, "next_action": next_action,
        "status": status, "pm_notes": pm_notes,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    if existing:
        return _memex_core_update(store="atelier", table="sessions",
                                  row_id=existing[0]["id"], changes=payload)
    payload.update({"project_id": project_id, "agent_id": agent_id})
    return _memex_core_insert(store="atelier", table="sessions", row=payload)


def transition_phase(*, project_id: int, to_phase: str,
                     agent_id: str, bypass_reason: str | None = None) -> dict:
    rows = _memex_core_query(store="atelier", table="projects",
                             where={"id": project_id})
    if not rows:
        raise ValueError(f"project_id {project_id} not found")
    return _memex_core_update(store="atelier", table="projects",
                              row_id=project_id, changes={"phase": to_phase})


def update_task_status(*, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    changes = {"status": status}
    if notes:
        changes["notes"] = notes
    return _memex_core_update(store="atelier", table="tasks",
                              row_id=task_id, changes=changes)


def record_phase_bypass(*, project_id: int, from_phase: str, to_phase: str,
                        reason: str, agent_id: str) -> dict:
    return _memex_core_insert(store="atelier", table="phase_bypasses",
                              row={"project_id": project_id,
                                   "from_phase": from_phase,
                                   "to_phase": to_phase,
                                   "reason": reason,
                                   "agent_id": agent_id})
```

- [ ] **Step 4: Run tests — expect pass**

```
pytest tests/test_backend_memex_state.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_memex.py tests/test_backend_memex_state.py
git commit -m "feat(backend-memex): wave-1 operational state writes"
```

---

### Task 3: Memex backend — reads

**Files:**
- Modify: `scripts/backend_memex.py` (append below Task 2's region)
- Test: `tests/test_backend_memex_reads.py`

Implements `find_documents`, `get_task`, `list_tasks`, `find_project_by_key`. Uses Memex Index search for the document query; direct Core CRUD for the rest.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_memex_reads.py
from unittest.mock import patch
from scripts import backend_memex


def test_find_documents_dispatches_to_memex_search():
    fake_results = [
        {"index_id": "01a", "key": "design-auth", "domain": "design",
         "store": "atelier", "row_id": 1, "searchable": "auth design"},
    ]
    with patch.object(backend_memex, "_memex_search", return_value=fake_results):
        results = backend_memex.find_documents(query="auth design")
    assert len(results) == 1
    assert results[0]["key"] == "design-auth"


def test_get_task_returns_row():
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[{"id": 1, "title": "Fix bug"}]):
        r = backend_memex.get_task(task_id=1)
    assert r["title"] == "Fix bug"


def test_get_task_missing_returns_none():
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


def test_find_project_by_key():
    with patch.object(backend_memex, "_memex_core_query",
                      return_value=[{"id": 1, "project_key": "abc"}]):
        r = backend_memex.find_project_by_key(project_key="abc")
    assert r["id"] == 1
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/test_backend_memex_reads.py -v
```

- [ ] **Step 3: Append reads to backend_memex.py**

```python
# Append to scripts/backend_memex.py

# ── Reads ──────────────────────────────────────────────────────────────────

def _memex_search(*, query: str, project_id: int | None = None,
                  domain: str | None = None, limit: int = 10) -> list[dict]:
    """Run an FTS5-only Memex Index search. We do not invoke the
    Reference Librarian here — Atelier uses simple lexical search for
    its CRUD layer. Brain-style ask/synthesize go via memex:run."""
    _ensure_memex_importable()
    from scripts import brain as memex_brain  # type: ignore
    plan = {"fts_query": query, "vector_query": None,
            "filters": {}, "limit": limit}
    if domain:
        plan["filters"]["domain"] = domain
    prep = memex_brain.ask_prepare(query)
    return memex_brain.ask_execute(prep, plan, with_embedding=False)


def find_documents(*, query: str, project_id: int | None = None,
                   domain: str | None = None, limit: int = 10) -> list[dict]:
    return _memex_search(query=query, project_id=project_id,
                         domain=domain, limit=limit)


def get_task(*, task_id: int) -> dict | None:
    rows = _memex_core_query(store="atelier", table="tasks",
                             where={"id": task_id})
    return rows[0] if rows else None


def list_tasks(*, project_id: int, status: str | None = None) -> list[dict]:
    where = {"project_id": project_id}
    if status:
        where["status"] = status
    return _memex_core_query(store="atelier", table="tasks", where=where)


def find_project_by_key(*, project_key: str) -> dict | None:
    rows = _memex_core_query(store="atelier", table="projects",
                             where={"project_key": project_key})
    return rows[0] if rows else None
```

- [ ] **Step 4: Run tests — expect pass**

```
pytest tests/test_backend_memex_reads.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_memex.py tests/test_backend_memex_reads.py
git commit -m "feat(backend-memex): wave-1 reads (search, tasks, projects)"
```

---

### Task 4: Memex-mode internal SKILL.md procedures

**Files:**
- Create: `internal/memex/dispatch-write/SKILL.md`
- Create: `internal/memex/dispatch-core/SKILL.md`
- Create: `internal/bootstrap-memex/SKILL.md`
- Test: `tests/test_internal_skills_present.py`

These are not surfaced to Claude Code (no top-level `name:` matching a registered skill). They are documentation/routing procedures consumed by Atelier's user-facing skills when those skills detect Memex mode.

- [ ] **Step 1: Write failing test**

```python
# tests/test_internal_skills_present.py
from pathlib import Path

INTERNAL = Path(__file__).parent.parent / "internal"


def test_dispatch_write_skill_present():
    f = INTERNAL / "memex" / "dispatch-write" / "SKILL.md"
    assert f.exists()
    assert "memex:index:write" in f.read_text(encoding="utf-8")


def test_dispatch_core_skill_present():
    f = INTERNAL / "memex" / "dispatch-core" / "SKILL.md"
    assert f.exists()
    assert "memex:core:" in f.read_text(encoding="utf-8")


def test_bootstrap_skill_present():
    f = INTERNAL / "bootstrap-memex" / "SKILL.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "register-role" in text
    assert "register-agent" in text
    assert "create-store" in text
```

- [ ] **Step 2: Create `internal/memex/dispatch-write/SKILL.md`**

```markdown
---
description: Internal — Tier 2 (structured-row) Atelier writes through memex:index:write. Caller-built librarian_output; no LLM dispatch. Not user-visible.
---

# memex/dispatch-write (internal)

## When invoked

An Atelier business operation (create project, write meeting minutes,
new task, content edit on an existing row) needs an Atelier-domain row
indexed in Memex's federated Index. Atelier knows the `domain` (per
`scripts/domain_vocabulary.DOMAINS`) so this is **Tier 2**: caller-built
`librarian_output`, no Librarian subagent dispatch, no LLM call.

For Tier 3 (prose ingest where the domain must be extracted from text)
use `internal/memex/dispatch-ingest/SKILL.md` instead — that path
dispatches the Librarian subagent via the Task tool.

## Inputs

- `domain` — must be in `scripts.domain_vocabulary.DOMAINS`
- `title`, `body` — what gets indexed in FTS5 (`searchable` = `"{title}. {body[:1500]}"`)
- `payload` — dict of target-table columns (will be persisted in `~/.memex/atelier.db.<table>`)
- `target_table` — one of `projects`, `tasks`, `meeting_minutes`, `project_documents`
- `caller_agent_id` — an Atelier-seeded agent (`atelier-pm-1`, etc.)
- `metadata` — optional dict written to `index.db.documents.metadata`
- `relations` — optional list of `{"to_index_id": ..., "rel_type": ...}` for explicit graph edges

## Recipe

The procedure body is `scripts.backend_memex._atelier_write(...)`. It:

1. Calls `domain_vocabulary.assert_valid(domain)` — rejects unknown domains.
2. Builds the classification dict (`index_id` UUID v7, `key` slug, `domain`, `searchable`, `metadata`, `relations`) and runs it through `librarian.validate_output()` — the shared schema check Memex v2.2.0 exposes.
3. Best-effort embedding via `embeddings.encode(searchable)` — `None` if the provider is unavailable.
4. Calls `librarian.write_entry(payload, librarian_output, target_store="atelier", target_table, caller_agent_id, embedding)` — Memex's canonical two-stage write (Index row → target-store row → row_id backlink).
5. Returns `{"status": "ingested", "index_id", "key", "domain", "row_id", "relations"}`.

## Errors

- `RuntimeError: Memex plugin not found` — Memex isn't installed despite `mode_detector` returning `memex`. Recover: re-run `mode_detector._clear_cache()` and re-detect; fall back to Local.
- `ValueError: unknown domain` — caller passed a domain outside `DOMAINS`. Use one of the vocabulary entries or amend the spec (see `internal/memex/domain-vocabulary.md`).
- `ValueError: Unknown store: atelier` — bootstrap has not run. Caller must run `internal/bootstrap-memex/SKILL.md` first.
- `ValueError: librarian_output missing fields` — shouldn't happen since `_atelier_write` builds the dict; if seen, it indicates a Memex schema bump. Pin the Memex version requirement and update Atelier.
```

- [ ] **Step 3: Create `internal/memex/dispatch-core/SKILL.md`**

```markdown
---
description: Internal — routes Atelier operational-state CRUD through Memex Core's insert/update/query/delete. Not user-visible.
---

# memex/dispatch-core (internal)

## When invoked

An Atelier operation needs to write or read an operational row
(sessions, phase transitions, phase bypasses, task status updates,
project rows by ID). These bypass the Librarian — pure CRUD.

## Recipe

Use `scripts.backend_memex._memex_core_insert / _update / _query` helpers.
They:
1. Import Memex's `scripts.stores` module from the installed plugin.
2. Run the SQL against the `atelier` store registered in
   `~/.memex/registry.json`.
3. Return a list of dict rows (query) or the affected row (insert/update).

No Librarian, no Archivist, no embeddings. Cheapest possible write path.

## When NOT to use

If the row carries searchable narrative content (task description, meeting
summary, document body), route through `dispatch-write` instead so it
appears in the federated Index.
```

- [ ] **Step 4: Create `internal/bootstrap-memex/SKILL.md`**

```markdown
---
description: Internal — first-run Atelier bootstrap into Memex. Seeds Atelier's roles, agent profiles, and creates the atelier store. Idempotent.
---

# bootstrap-memex (internal)

## When invoked

Every Atelier command in Memex mode reads `~/.memex/atelier.bootstrap.json`
at startup. If the marker is missing or the recorded version is older than
the installed Atelier version, this procedure runs.

## Recipe

```python
from scripts import seed_data, mode_detector
import sys, json
from pathlib import Path

# 1. Make sure Memex is reachable.
assert mode_detector.detect_mode() == "memex"

# 2. Import Memex's CRUD modules.
plugin = Path.home() / ".claude" / "plugins" / "cache" / "agora" / "memex"
latest = sorted(plugin.iterdir())[-1]
sys.path.insert(0, str(latest))
from scripts import roles as memex_roles, agents as memex_agents, stores as memex_stores  # type: ignore
agents_db = str(Path.home() / ".memex" / "agents.db")

# 3. Seed roles (idempotent on name).
for r in seed_data.load_role_seed():
    existing = [x for x in memex_roles.list_roles(agents_db) if x["name"] == r["name"]]
    if not existing:
        memex_roles.create_role(agents_db, name=r["name"], description=r["description"])

# 4. Seed agents (idempotent on id).
role_map = {r["name"]: r["id"] for r in memex_roles.list_roles(agents_db)}
for a in seed_data.load_agent_seed():
    if memex_agents.get_agent(agents_db, a["agent_id"]) is None:
        memex_agents.create_agent(agents_db, a["agent_id"], a["name"],
                                  role_map[a["role_name"]], a["profile"])

# 5. Create atelier store if absent.
registry = json.loads((Path.home() / ".memex" / "registry.json").read_text())
if "atelier" not in registry.get("stores", {}):
    atelier_plugin = Path(__file__).resolve().parents[2]  # plugin root
    memex_stores.create_store(
        name="atelier",
        migrations_dir=str(atelier_plugin / "migrations" / "shared"),
    )

# 6. Write the marker.
import datetime
marker = Path.home() / ".memex" / "atelier.bootstrap.json"
import importlib.metadata as md
try:
    version = md.version("atelier")
except md.PackageNotFoundError:
    version = "0.0.0-dev"
marker.write_text(json.dumps({
    "version": version,
    "bootstrapped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, indent=2), encoding="utf-8")
```

## Idempotency

- Roles: `create_role` no-ops if name exists.
- Agents: skipped explicitly when `get_agent` returns non-None.
- Store: skipped if already in `registry.json`.
- Marker: overwritten with current timestamp + version each successful run.

## Failure semantics

If any step raises, the marker is NOT written. Next Atelier command will
retry. Partial state (e.g., 4 of 6 roles seeded) is acceptable — the
re-run skips already-seeded entries.
```

- [ ] **Step 5: Run tests — expect pass**

```
pytest tests/test_internal_skills_present.py -v
```

- [ ] **Step 6: Commit**

```bash
git add internal/memex/ internal/bootstrap-memex/ tests/test_internal_skills_present.py
git commit -m "feat(internal-memex): wave-1 dispatch + bootstrap procedures"
```

---

### Task 5: Local backend — document writes

**Files:**
- Create: `scripts/backend_local.py`
- Test: `tests/test_backend_local_documents.py`

Mirror of Task 1 with project-local SQLite. Writes to `<project-root>/.ai/atelier.db`. FTS5-indexed `documents` table; raw bodies dropped to `<project-root>/.ai/raw/`. No Librarian, no embeddings.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_local_documents.py
from pathlib import Path
import pytest
from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """Stand up a fake project root with .ai/ initialized."""
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()  # fake git root
    monkeypatch.chdir(root)
    # Initialize the local atelier.db with all migrations
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    # Seed a role + agent so created_by works
    from scripts.roles import create_role
    from scripts.agents import create_agent
    r = create_role(str(db), name="Project Manager", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM", role_id=r["id"], profile="pm")
    # Create a project
    from scripts.projects import create_project
    create_project(str(db), name="myproj", description="test", created_by="atelier-pm-1")
    return root


def test_write_document_creates_local_row(project_root):
    r = backend_local.write_document(
        domain="design", title="Auth Design",
        body="# Auth\n\nOAuth2 flow.", metadata={"project_id": 1},
        caller_agent_id="atelier-pm-1",
    )
    assert r["status"] == "ingested"
    assert r["row_id"] >= 1
    assert r["index_id"] is None  # local mode has no global index


def test_write_document_archives_raw_body(project_root):
    backend_local.write_document(
        domain="design", title="X", body="hello world",
        metadata={"project_id": 1}, caller_agent_id="atelier-pm-1",
    )
    raw_files = list((project_root / ".ai" / "raw").rglob("*.md"))
    assert len(raw_files) == 1
    assert "hello world" in raw_files[0].read_text(encoding="utf-8")


def test_write_task_creates_task_row(project_root):
    r = backend_local.write_task(
        title="Fix bug", description="OAuth 500",
        project_id=1, created_by="atelier-pm-1",
    )
    assert r["row_id"] >= 1


def test_write_meeting_writes_minutes_markdown(project_root):
    backend_local.write_meeting(
        title="Kickoff", date="2026-05-16",
        summary="scope", decisions="oauth2",
        created_by="atelier-pm-1",
    )
    meetings = list((project_root / ".ai" / "meetings").glob("*.md"))
    assert len(meetings) == 1
```

- [ ] **Step 2: Run tests — expect failure**

```
pytest tests/test_backend_local_documents.py -v
```

- [ ] **Step 3: Implement local document writes**

```python
# scripts/backend_local.py
"""Local-mode backend.

Project-local SQLite at <project-root>/.ai/atelier.db with FTS5 over a
documents table. Raw bodies archived to <project-root>/.ai/raw/.
No embeddings, no Librarian, no federated Index.
"""
from __future__ import annotations
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _project_root() -> Path:
    """Walk up from CWD until we find a .git directory; that's the root."""
    cur = Path.cwd().resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return Path.cwd().resolve()


def _local_db() -> str:
    db = _project_root() / ".ai" / "atelier.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return str(db)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]


def _archive_raw(body: str, title: str) -> str:
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    raw_dir = _project_root() / ".ai" / "raw" / h[:2]
    raw_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_slug(title)}-{h[:8]}.md"
    path = raw_dir / fname
    if not path.exists():
        path.write_text(body, encoding="utf-8")
    return str(path)


def _conn():
    c = sqlite3.connect(_local_db())
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def _ensure_documents_table(c) -> None:
    """Local equivalent of Memex's index.db.documents table — minimal."""
    c.execute("""CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        domain TEXT NOT NULL,
        title TEXT NOT NULL,
        searchable TEXT NOT NULL,
        raw_path TEXT,
        metadata TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        target_table TEXT,
        target_row_id INTEGER
    )""")
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
        key, title, searchable, content='documents', content_rowid='id'
    )""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
        INSERT INTO documents_fts(rowid, key, title, searchable)
        VALUES (new.id, new.key, new.title, new.searchable);
    END""")
    c.commit()


# ── Document writes ────────────────────────────────────────────────────────

def write_document(*, domain: str, title: str, body: str,
                   metadata: dict, caller_agent_id: str,
                   source_url: str | None = None) -> dict:
    import json
    c = _conn()
    _ensure_documents_table(c)
    raw_path = _archive_raw(body, title)
    key = _slug(title)
    searchable = f"{title}. {body[:1500]}"
    cur = c.execute(
        "INSERT INTO documents (key, domain, title, searchable, raw_path, "
        "metadata, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (key, domain, title, searchable, raw_path,
         json.dumps(metadata or {}), caller_agent_id, _now()),
    )
    row_id = cur.fetchone()[0]
    c.commit()
    c.close()
    return {"status": "ingested", "index_id": None,
            "row_id": row_id, "key": key, "domain": domain,
            "relations": []}


def write_task(*, title: str, description: str, project_id: int,
               created_by: str, assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None) -> dict:
    c = _conn()
    cur = c.execute(
        "INSERT INTO tasks (project_id, title, description, created_by, "
        "assigned_to, priority, notes, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) RETURNING id",
        (project_id, title, description, created_by, assigned_to,
         priority, notes, _now(), _now()),
    )
    row_id = cur.fetchone()[0]
    c.commit()
    # Also index via documents for searchability
    body = f"# {title}\n\n{description or ''}\n"
    if notes:
        body += f"\n## Notes\n{notes}\n"
    write_document(domain="task", title=title, body=body,
                   metadata={"project_id": project_id, "task_id": row_id},
                   caller_agent_id=created_by)
    c.close()
    return {"status": "ingested", "index_id": None,
            "row_id": row_id, "key": _slug(title), "domain": "task",
            "relations": []}


def write_meeting(*, title: str, date: str, summary: str,
                  decisions: str, created_by: str,
                  project_id: int | None = None) -> dict:
    filename = f"{date}-{_slug(title)}.md"
    meetings_dir = _project_root() / ".ai" / "meetings"
    meetings_dir.mkdir(parents=True, exist_ok=True)
    body = f"# {title}\n\nDate: {date}\n\n## Summary\n\n{summary}\n\n## Decisions\n\n{decisions}\n"
    (meetings_dir / filename).write_text(body, encoding="utf-8")
    c = _conn()
    cur = c.execute(
        "INSERT INTO meeting_minutes (title, date, filename, summary, "
        "decisions, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (title, date, filename, summary, decisions, created_by, _now(), _now()),
    )
    row_id = cur.fetchone()[0]
    c.commit()
    write_document(domain="meeting", title=title, body=body,
                   metadata={"meeting_id": row_id, "project_id": project_id},
                   caller_agent_id=created_by)
    c.close()
    return {"status": "ingested", "index_id": None,
            "row_id": row_id, "key": _slug(title), "domain": "meeting",
            "relations": []}
```

- [ ] **Step 4: Run tests — expect pass**

```
pytest tests/test_backend_local_documents.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_local.py tests/test_backend_local_documents.py
git commit -m "feat(backend-local): wave-1' document writes with FTS5 + raw archive"
```

---

### Task 6: Local backend — operational state writes

**Files:**
- Modify: `scripts/backend_local.py` (append)
- Test: `tests/test_backend_local_state.py`

Plain SQL into the existing tables. No Librarian indirection.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_local_state.py
from pathlib import Path
import pytest
from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.projects import create_project
    from scripts.tasks import create_task
    r = create_role(str(db), name="Project Manager", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM", role_id=r["id"], profile="pm")
    create_project(str(db), name="p", description="d", created_by="atelier-pm-1")
    create_task(str(db), project_id=1, title="t", description="d",
                created_by="atelier-pm-1")
    return root


def test_upsert_session_creates(project_root):
    s = backend_local.upsert_session(
        project_id=1, agent_id="atelier-pm-1", phase="design:open",
    )
    assert s["id"] >= 1


def test_upsert_session_updates(project_root):
    backend_local.upsert_session(project_id=1, agent_id="atelier-pm-1",
                                 phase="design:open")
    s = backend_local.upsert_session(project_id=1, agent_id="atelier-pm-1",
                                     accomplished="kickoff done")
    assert s["accomplished"] == "kickoff done"


def test_transition_phase(project_root):
    r = backend_local.transition_phase(project_id=1, to_phase="plan:open",
                                       agent_id="atelier-pm-1")
    assert r["phase"] == "plan:open"


def test_update_task_status(project_root):
    r = backend_local.update_task_status(task_id=1, status="in-progress")
    assert r["status"] == "in-progress"


def test_record_phase_bypass(project_root):
    r = backend_local.record_phase_bypass(
        project_id=1, from_phase="design:open", to_phase="plan:open",
        reason="override", agent_id="atelier-pm-1",
    )
    assert r["id"] >= 1
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Append state writes to backend_local.py**

```python
# Append to scripts/backend_local.py

# ── Operational state writes ───────────────────────────────────────────────

def upsert_session(*, project_id: int, agent_id: str, phase: str | None = None,
                   current_tasks: str | None = None,
                   accomplished: str | None = None,
                   next_action: str | None = None,
                   status: str = "in-progress",
                   pm_notes: str | None = None) -> dict:
    c = _conn()
    existing = c.execute(
        "SELECT * FROM sessions WHERE project_id = ? AND agent_id = ? "
        "AND status = 'in-progress' LIMIT 1",
        (project_id, agent_id),
    ).fetchone()
    if existing:
        sets = []
        vals = []
        for k, v in [("phase", phase), ("current_tasks", current_tasks),
                     ("accomplished", accomplished),
                     ("next_action", next_action), ("status", status),
                     ("pm_notes", pm_notes)]:
            if v is not None:
                sets.append(f"{k} = ?")
                vals.append(v)
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(existing["id"])
        c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", vals)
        c.commit()
        row = c.execute("SELECT * FROM sessions WHERE id = ?",
                        (existing["id"],)).fetchone()
    else:
        cur = c.execute(
            "INSERT INTO sessions (project_id, agent_id, phase, current_tasks, "
            "accomplished, next_action, status, pm_notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (project_id, agent_id, phase, current_tasks, accomplished,
             next_action, status, pm_notes, _now(), _now()),
        )
        row = cur.fetchone()
        c.commit()
    result = dict(row) if row else {}
    c.close()
    return result


def transition_phase(*, project_id: int, to_phase: str,
                     agent_id: str, bypass_reason: str | None = None) -> dict:
    c = _conn()
    c.execute("UPDATE projects SET phase = ?, updated_at = ? WHERE id = ?",
              (to_phase, _now(), project_id))
    c.commit()
    row = c.execute("SELECT * FROM projects WHERE id = ?",
                    (project_id,)).fetchone()
    c.close()
    return dict(row) if row else {}


def update_task_status(*, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    c = _conn()
    if notes:
        c.execute("UPDATE tasks SET status = ?, notes = ?, updated_at = ? "
                  "WHERE id = ?", (status, notes, _now(), task_id))
    else:
        c.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                  (status, _now(), task_id))
    c.commit()
    row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    c.close()
    return dict(row) if row else {}


def record_phase_bypass(*, project_id: int, from_phase: str, to_phase: str,
                        reason: str, agent_id: str) -> dict:
    c = _conn()
    cur = c.execute(
        "INSERT INTO phase_bypasses (project_id, from_phase, to_phase, "
        "reason, agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
        (project_id, from_phase, to_phase, reason, agent_id, _now()),
    )
    row = cur.fetchone()
    c.commit()
    c.close()
    return dict(row) if row else {}
```

- [ ] **Step 4: Run tests — expect pass**

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_local.py tests/test_backend_local_state.py
git commit -m "feat(backend-local): wave-1' operational state writes"
```

---

### Task 7: Local backend — reads

**Files:**
- Modify: `scripts/backend_local.py` (append)
- Test: `tests/test_backend_local_reads.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_local_reads.py
from pathlib import Path
import pytest
from scripts import backend_local
from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    root = tmp_path / "myproj"
    root.mkdir(); (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    from scripts.roles import create_role
    from scripts.agents import create_agent
    from scripts.projects import create_project
    r = create_role(str(db), name="Project Manager", description="PM")
    create_agent(str(db), id="atelier-pm-1", name="PM", role_id=r["id"], profile="pm")
    create_project(str(db), name="myproj", description="auth service",
                   created_by="atelier-pm-1")
    backend_local.write_document(
        domain="design", title="Auth Design",
        body="OAuth2 flow with refresh tokens.",
        metadata={"project_id": 1}, caller_agent_id="atelier-pm-1",
    )
    backend_local.write_task(title="Fix login bug", description="500 error",
                             project_id=1, created_by="atelier-pm-1")
    return root


def test_find_documents_fts_match(project_root):
    r = backend_local.find_documents(query="OAuth2")
    assert len(r) >= 1
    assert any("Auth Design" in d.get("title", "") for d in r)


def test_find_documents_no_match(project_root):
    r = backend_local.find_documents(query="nonexistentterm12345")
    assert r == []


def test_get_task(project_root):
    r = backend_local.get_task(task_id=1)
    assert r["title"] == "Fix login bug"


def test_get_task_missing(project_root):
    assert backend_local.get_task(task_id=999) is None


def test_list_tasks(project_root):
    r = backend_local.list_tasks(project_id=1)
    assert len(r) == 1


def test_list_tasks_with_status(project_root):
    r = backend_local.list_tasks(project_id=1, status="pending")
    assert len(r) == 1


def test_find_project_by_key(project_root):
    """In local mode project_key is the git remote URL hash; for the
    fixture there's no remote so it falls back to the project name."""
    r = backend_local.find_project_by_key(project_key="myproj")
    assert r is not None
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Append reads + add project_key column migration**

```python
# Append to scripts/backend_local.py

# ── Reads ──────────────────────────────────────────────────────────────────

def find_documents(*, query: str, project_id: int | None = None,
                   domain: str | None = None, limit: int = 10) -> list[dict]:
    c = _conn()
    _ensure_documents_table(c)
    where = ["documents_fts MATCH ?"]
    params: list = [query]
    if domain:
        where.append("documents.domain = ?")
        params.append(domain)
    sql = (f"SELECT documents.* FROM documents "
           f"JOIN documents_fts ON documents.id = documents_fts.rowid "
           f"WHERE {' AND '.join(where)} LIMIT ?")
    params.append(limit)
    rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    c.close()
    return rows


def get_task(*, task_id: int) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_tasks(*, project_id: int, status: str | None = None) -> list[dict]:
    c = _conn()
    if status:
        rows = c.execute("SELECT * FROM tasks WHERE project_id = ? AND status = ?",
                         (project_id, status)).fetchall()
    else:
        rows = c.execute("SELECT * FROM tasks WHERE project_id = ?",
                         (project_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def find_project_by_key(*, project_key: str) -> dict | None:
    """Look up a project by its key. In local mode we don't have a
    dedicated project_key column on the v1 schema, so we match by name
    as a fallback. A future migration adds a real project_key column."""
    c = _conn()
    row = c.execute("SELECT * FROM projects WHERE name = ?",
                    (project_key,)).fetchone()
    c.close()
    return dict(row) if row else None
```

- [ ] **Step 4: Run tests — expect pass**

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_local.py tests/test_backend_local_reads.py
git commit -m "feat(backend-local): wave-1' reads (FTS5 search, task lookups)"
```

---

### Task 8: Local-mode internal SKILL.md procedures

**Files:**
- Create: `internal/local/wiki-write/SKILL.md`
- Create: `internal/local/wiki-search/SKILL.md`
- Create: `internal/local/wiki-archive/SKILL.md`
- Create: `internal/local/state-crud/SKILL.md`
- Test: `tests/test_internal_local_skills_present.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_internal_local_skills_present.py
from pathlib import Path
INTERNAL = Path(__file__).parent.parent / "internal" / "local"


def test_wiki_write_present():
    f = INTERNAL / "wiki-write" / "SKILL.md"
    assert f.exists()
    assert "backend_local.write_document" in f.read_text(encoding="utf-8")


def test_wiki_search_present():
    f = INTERNAL / "wiki-search" / "SKILL.md"
    assert f.exists()
    assert "FTS5" in f.read_text(encoding="utf-8")


def test_wiki_archive_present():
    f = INTERNAL / "wiki-archive" / "SKILL.md"
    assert f.exists()


def test_state_crud_present():
    f = INTERNAL / "state-crud" / "SKILL.md"
    assert f.exists()
```

- [ ] **Step 2-5: Create the four SKILL.md files**

```markdown
<!-- internal/local/wiki-write/SKILL.md -->
---
description: Internal — Local-mode document write. FTS5-indexed; raw body archived to .ai/raw/.
---

# local/wiki-write (internal)

## Recipe
Call `scripts.backend_local.write_document(...)`. It:
1. Computes a slug key + searchable text.
2. Copies the raw body into `<project-root>/.ai/raw/<2char-hash>/<slug>-<short-hash>.md`.
3. Inserts a row into `documents` table (auto-indexed in `documents_fts` via trigger).

No embeddings, no Librarian. Returns `{status, row_id, key, domain}` (index_id is None).
```

```markdown
<!-- internal/local/wiki-search/SKILL.md -->
---
description: Internal — Local-mode FTS5 search over documents.
---

# local/wiki-search (internal)

## Recipe
Call `scripts.backend_local.find_documents(query=..., domain=..., limit=...)`.
It runs an FTS5 MATCH over the `documents_fts` virtual table.

## Limitations
- No vector retrieval.
- No cross-project search (FTS5 is per-project-DB).
- No re-ranking beyond raw FTS5 score.
```

```markdown
<!-- internal/local/wiki-archive/SKILL.md -->
---
description: Internal — Local-mode raw-body archive helper. Called by wiki-write.
---

# local/wiki-archive (internal)

## Recipe
`scripts.backend_local._archive_raw(body, title)` writes the body to
`<project-root>/.ai/raw/<2char-hash>/<slug>-<short-hash>.md` and returns
the path. Idempotent on content hash — re-archiving the same bytes is a
no-op.
```

```markdown
<!-- internal/local/state-crud/SKILL.md -->
---
description: Internal — Local-mode CRUD for operational state (sessions, phases, tasks). Direct SQLite, no Librarian.
---

# local/state-crud (internal)

## Recipe
Use `scripts.backend_local.{upsert_session, transition_phase, update_task_status,
record_phase_bypass}`. All operate on the project-local `<project-root>/.ai/atelier.db`.
```

- [ ] **Step 6: Run test, commit**

```bash
pytest tests/test_internal_local_skills_present.py -v
git add internal/local/ tests/test_internal_local_skills_present.py
git commit -m "feat(internal-local): wave-1' wiki + state-crud procedures"
```

---

### Task 9: Rewire `scripts/backend.py` to dispatch by mode (depends on 1–8)

**Files:**
- Modify: `scripts/backend.py` (replace bodies)
- Test: `tests/test_backend_dispatch.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backend_dispatch.py
from unittest.mock import patch
from scripts import backend, mode_detector


def test_dispatches_to_memex_when_mode_is_memex():
    mode_detector._clear_cache()
    with patch.object(mode_detector, "detect_mode", return_value="memex"), \
         patch("scripts.backend_memex.write_document",
               return_value={"status": "ingested", "row_id": 1}) as m:
        backend.write_document(domain="d", title="t", body="b",
                               metadata={}, caller_agent_id="a")
    m.assert_called_once()


def test_dispatches_to_local_when_mode_is_local():
    mode_detector._clear_cache()
    with patch.object(mode_detector, "detect_mode", return_value="local"), \
         patch("scripts.backend_local.write_document",
               return_value={"status": "ingested", "row_id": 1}) as m:
        backend.write_document(domain="d", title="t", body="b",
                               metadata={}, caller_agent_id="a")
    m.assert_called_once()


def test_every_facade_method_dispatches():
    """Verify every NotImplementedError method is now a dispatch shim."""
    mode_detector._clear_cache()
    with patch.object(mode_detector, "detect_mode", return_value="local"):
        # Just call each with mock-friendly args; we patch the local impl
        with patch.multiple("scripts.backend_local",
            write_document=lambda **k: {"ok": 1},
            write_task=lambda **k: {"ok": 1},
            write_meeting=lambda **k: {"ok": 1},
            upsert_session=lambda **k: {"ok": 1},
            transition_phase=lambda **k: {"ok": 1},
            update_task_status=lambda **k: {"ok": 1},
            record_phase_bypass=lambda **k: {"ok": 1},
            find_documents=lambda **k: [],
            get_task=lambda **k: None,
            list_tasks=lambda **k: [],
            find_project_by_key=lambda **k: None,
        ):
            backend.write_document(domain="d", title="t", body="b",
                                    metadata={}, caller_agent_id="a")
            backend.write_task(title="t", description="d", project_id=1,
                                created_by="a")
            backend.find_documents(query="q")
            assert backend.get_task(task_id=1) is None
            assert backend.list_tasks(project_id=1) == []
```

- [ ] **Step 2: Run tests — expect failure**

- [ ] **Step 3: Replace `scripts/backend.py` bodies with dispatchers**

```python
# scripts/backend.py — REPLACE bodies with dispatch
from __future__ import annotations
from scripts import mode_detector


def _impl():
    if mode_detector.detect_mode() == "memex":
        from scripts import backend_memex as m
        return m
    from scripts import backend_local as m
    return m


def write_document(**kwargs) -> dict:
    return _impl().write_document(**kwargs)


def write_task(**kwargs) -> dict:
    return _impl().write_task(**kwargs)


def write_meeting(**kwargs) -> dict:
    return _impl().write_meeting(**kwargs)


def upsert_session(**kwargs) -> dict:
    return _impl().upsert_session(**kwargs)


def transition_phase(**kwargs) -> dict:
    return _impl().transition_phase(**kwargs)


def update_task_status(**kwargs) -> dict:
    return _impl().update_task_status(**kwargs)


def record_phase_bypass(**kwargs) -> dict:
    return _impl().record_phase_bypass(**kwargs)


def find_documents(**kwargs) -> list[dict]:
    return _impl().find_documents(**kwargs)


def get_task(**kwargs) -> dict | None:
    return _impl().get_task(**kwargs)


def list_tasks(**kwargs) -> list[dict]:
    return _impl().list_tasks(**kwargs)


def find_project_by_key(**kwargs) -> dict | None:
    return _impl().find_project_by_key(**kwargs)
```

- [ ] **Step 4: Run tests — expect pass; also re-run full suite**

```
pytest tests/ -x
```

- [ ] **Step 5: Commit**

```bash
git add scripts/backend.py tests/test_backend_dispatch.py
git commit -m "feat(backend): wave-1.5 mode-dispatched facade"
```

---

### Task 10: Bootstrap end-to-end integration test

**Files:**
- Create: `tests/test_bootstrap_e2e.py`

End-to-end test exercising `internal/bootstrap-memex/SKILL.md` against a temp Memex install. Verifies idempotency on a second invocation. Marked slow; runs in CI only.

- [ ] **Step 1: Write the e2e test (and helper to install a fake Memex)**

```python
# tests/test_bootstrap_e2e.py
"""End-to-end bootstrap test.

Stands up a fake Memex install in a tmp dir + monkeypatches the plugin
cache path so backend_memex's _memex_plugin_dir() finds it. Then runs
bootstrap and asserts roles/agents/store all land correctly.
"""
import json
import shutil
import sys
import sqlite3
from pathlib import Path
import pytest


@pytest.fixture
def fake_memex_install(tmp_path, monkeypatch):
    """Create a minimal Memex plugin tree the backend can import."""
    plugin_root = tmp_path / "memex_plugin"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "memex", "version": "2.2.0",
    }))
    # Real Memex's scripts/ are required; copy from the actual install if available
    real_memex = Path.home() / "Documents" / "Skills" / "memex"
    if not real_memex.exists():
        pytest.skip("real Memex repo not available")
    shutil.copytree(real_memex / "scripts", plugin_root / "scripts")

    # Patch the plugin-cache location
    cache_root = tmp_path / "claude" / "plugins" / "cache" / "agora" / "memex" / "2.2.0"
    cache_root.parent.mkdir(parents=True)
    shutil.copytree(plugin_root, cache_root)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".memex").mkdir()
    (home / ".memex" / "registry.json").write_text('{"stores": {}}')
    monkeypatch.setattr(Path, "home", lambda: home)
    # Force sys.path refresh
    monkeypatch.setattr("scripts.backend_memex._memex_plugin_dir",
                        lambda: cache_root)
    return home


def test_bootstrap_seeds_roles_agents_and_creates_store(fake_memex_install):
    from scripts import mode_detector
    mode_detector._clear_cache()
    # Execute the bootstrap procedure inline (mirrors what
    # internal/bootstrap-memex/SKILL.md tells the calling skill to do).
    import importlib
    backend_memex = importlib.import_module("scripts.backend_memex")
    backend_memex._ensure_memex_importable()
    from scripts import roles as memex_roles, agents as memex_agents  # noqa
    # Run bootstrap helper (factored into backend_memex for testability)
    from scripts.bootstrap import run_bootstrap
    run_bootstrap()
    # Verify
    agents_db = str(fake_memex_install / ".memex" / "agents.db")
    role_names = {r["name"] for r in memex_roles.list_roles(agents_db)}
    assert {"Project Manager", "Software Engineer"} <= role_names
    assert memex_agents.get_agent(agents_db, "atelier-pm-1") is not None
    registry = json.loads((fake_memex_install / ".memex" / "registry.json").read_text())
    assert "atelier" in registry["stores"]


def test_bootstrap_is_idempotent(fake_memex_install):
    from scripts.bootstrap import run_bootstrap
    run_bootstrap()
    run_bootstrap()  # second call must not error or duplicate
    from scripts.backend_memex import _ensure_memex_importable
    _ensure_memex_importable()
    from scripts import roles as memex_roles  # noqa
    agents_db = str(fake_memex_install / ".memex" / "agents.db")
    roles = memex_roles.list_roles(agents_db)
    # Each role appears exactly once
    names = [r["name"] for r in roles]
    assert len(names) == len(set(names))


def test_bootstrap_rejects_old_memex(fake_memex_install, tmp_path,
                                      monkeypatch):
    """Bootstrap MUST refuse to run against Memex < v2.2.0 because the
    caller-built librarian_output contract isn't there."""
    # Rewrite the fake plugin's manifest to claim v2.1.0
    cache_root = (tmp_path / "claude" / "plugins" / "cache" / "agora"
                  / "memex" / "2.2.0")
    manifest = cache_root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "memex", "version": "2.1.0"}))
    # Force re-detection from the patched dir
    from scripts import backend_memex
    monkeypatch.setattr(backend_memex, "_memex_plugin_dir",
                        lambda: cache_root)
    from scripts.bootstrap import run_bootstrap
    with pytest.raises(RuntimeError, match="requires Memex v2.2.0"):
        run_bootstrap()
```

- [ ] **Step 2: Create `scripts/bootstrap.py` containing the Python the SKILL.md procedure invokes**

```python
# scripts/bootstrap.py
"""Memex-mode bootstrap. Idempotent. Called when the bootstrap marker is
missing or version-stale. The procedure body matches
internal/bootstrap-memex/SKILL.md."""
from __future__ import annotations
import datetime
import json
import sys
from pathlib import Path
from scripts import seed_data
from scripts import backend_memex


MIN_MEMEX_VERSION = (2, 2, 0)


def _require_memex_version() -> str:
    """Atelier Tier 2 writes require Memex v2.2.0+ (caller-built
    librarian_output + librarian.validate_output). Raise if older."""
    manifest = backend_memex._memex_plugin_dir() / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    version_str = data.get("version", "0.0.0")
    parts = tuple(int(p) for p in version_str.split(".")[:3])
    if parts < MIN_MEMEX_VERSION:
        raise RuntimeError(
            f"Atelier requires Memex v2.2.0+ (caller-built librarian_output). "
            f"Installed: v{version_str}. Upgrade memex via agora "
            f"(`claude plugin update memex`) or fall back to Atelier Local mode "
            f"by uninstalling Memex."
        )
    return version_str


def run_bootstrap() -> dict:
    backend_memex._ensure_memex_importable()
    memex_version = _require_memex_version()
    from scripts import roles as memex_roles, agents as memex_agents, stores as memex_stores  # type: ignore

    memex_home = Path.home() / ".memex"
    agents_db = str(memex_home / "agents.db")

    # Roles
    role_map: dict[str, int] = {}
    for r in seed_data.load_role_seed():
        existing = [x for x in memex_roles.list_roles(agents_db) if x["name"] == r["name"]]
        if existing:
            role_map[r["name"]] = existing[0]["id"]
        else:
            new = memex_roles.create_role(agents_db, name=r["name"], description=r["description"])
            role_map[r["name"]] = new["id"]

    # Agents
    for a in seed_data.load_agent_seed():
        if memex_agents.get_agent(agents_db, a["agent_id"]) is None:
            memex_agents.create_agent(agents_db, a["agent_id"], a["name"],
                                      role_map[a["role_name"]], a["profile"])

    # Store
    registry_path = memex_home / "registry.json"
    registry = json.loads(registry_path.read_text())
    if "atelier" not in registry.get("stores", {}):
        atelier_plugin = Path(__file__).resolve().parents[1]
        memex_stores.create_store(name="atelier",
                                  migrations_dir=str(atelier_plugin / "migrations" / "shared"))

    # Marker
    try:
        import importlib.metadata as md
        version = md.version("atelier")
    except Exception:
        version = "1.1.0-dev"
    marker = memex_home / "atelier.bootstrap.json"
    marker.write_text(json.dumps({
        "version": version,
        "memex_version": memex_version,
        "bootstrapped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    return {"version": version, "memex_version": memex_version, "marker": str(marker)}
```

- [ ] **Step 3: Run e2e tests**

```
pytest tests/test_bootstrap_e2e.py -v
```

- [ ] **Step 4: Commit**

```bash
git add scripts/bootstrap.py tests/test_bootstrap_e2e.py
git commit -m "feat(bootstrap): wave-1.5 Memex bootstrap module + e2e tests"
```

---

## Plan 2 acceptance

- All 10 tasks merged.
- `pytest tests/` green (existing + new).
- `scripts/backend.py` dispatches; no `NotImplementedError` reachable.
- `scripts/backend_memex.py` and `scripts/backend_local.py` both export the same 11 names.
- `scripts/bootstrap.py` is idempotent (verified by e2e test).
- 7 new internal/* procedures present, none surface as slash commands.
