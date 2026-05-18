# Atelier ↔ Memex v2 Retrofit — Plan 2 of 4: Write Paths (Waves 1 + 1')

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the empty `backend.py` facade with two complete backends — Memex-mode (dispatches to `memex:run`) and Local-mode (direct SQLite). Add the internal SKILL.md routing procedures for both. End-state: every backend method returns real data on real stores.

**Architecture:** Two physically separate Python modules — `scripts/backend_memex.py` and `scripts/backend_local.py` — implement the contract from Plan 1. `scripts/backend.py` becomes a thin dispatcher selecting between them via `mode_detector.detect_mode()`. The two backends are file-disjoint so they can be implemented in parallel.

**Tech Stack:** Python 3.10+, pytest, sqlite3, JSON. Memex integration uses direct Python imports from the memex plugin (`from scripts.agents import librarian`, etc.) — plugin_root resolved via `~/.memex/config.json` and added to `sys.path`.

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
    patched in each test so we don't need a real Memex install.

    Note: registry.json is a flat map per memex/scripts/registry.py; the
    top-level keys ARE the store names (no `{"stores": {...}}` wrapper).
    """
    import json as _json
    from datetime import datetime as _datetime, timezone as _tz
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text(_json.dumps({
        "atelier": {
            "name": "atelier",
            "path": (memex_home / "atelier.db").as_posix(),
            "schema_version": "v1",
            "registered_at": _datetime.now(_tz.utc).isoformat(),
        },
    }))
    # Memex v2.5.0's memex_home() validates that ~/.memex is under $HOME.
    # The explicit env var is more reliable than monkeypatching Path.home.
    monkeypatch.setenv("MEMEX_HOME", str(memex_home))
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
    # Stub key-construction helpers so tests don't need a live atelier.db.
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: "myproj" if pid else None)
    monkeypatch.setattr(backend_memex, "_next_seq",
                        lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    result = backend_memex.write_document(
        domain="project_doc", title="Auth Design",
        body="OAuth2 flow with refresh tokens.",
        metadata={"project_id": 1, "filename": "DESIGN.md"},
        caller_agent_id="atelier-product-manager-1",
    )

    assert result["row_id"] == 42
    assert captured["target_store"] == "atelier"
    assert captured["target_table"] == "project_documents"
    assert captured["librarian_output"]["domain"] == "project_doc"
    # Canonical key shape per spec §6.7:
    # <workspace>/<project>/<domain>/<date>-<title>-<seq>
    key = captured["librarian_output"]["key"]
    assert key.startswith("atelier/myproj/project_doc/")
    assert key.endswith("-auth-design-1")
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
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: "myproj" if pid else None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    backend_memex.write_task(
        title="Fix auth bug", description="OAuth returns 500",
        project_id=1, created_by="atelier-engineer-1",
        priority=5, notes="repro: hit /oauth/callback twice",
    )
    assert captured["target_table"] == "tasks"
    assert captured["librarian_output"]["domain"] == "task"
    assert captured["payload"]["priority"] == 5
    assert "repro" in captured["payload"]["notes"]
    # Spec §6.8: searchable includes metadata narrative (notes).
    assert "repro" in captured["librarian_output"]["searchable"]


def test_write_project_targets_projects_table(fake_memex, monkeypatch):
    """`write_project` is a distinct facade method (user decision +
    spec §4.3). Domain pinned to `project`, target table `projects`,
    title-as-name, description-as-body."""
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry",
        lambda **k: (captured.update(k), {"status": "ingested",
                                          "index_id": k["librarian_output"]["index_id"],
                                          "key": k["librarian_output"]["key"],
                                          "domain": "project",
                                          "row_id": 1, "relations": []})[1])
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: None)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))
    monkeypatch.setattr(backend_memex, "_next_seq",
                        lambda *a, **k: 1)

    backend_memex.write_project(
        workspace_id=1, slug="auth-svc", name="Auth Service",
        description="OAuth2 + refresh tokens.",
        created_by="atelier-product-manager-1",
    )
    assert captured["target_table"] == "projects"
    assert captured["librarian_output"]["domain"] == "project"
    assert captured["payload"]["slug"] == "auth-svc"
    assert captured["payload"]["name"] == "Auth Service"


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
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    backend_memex.write_meeting(
        title="Kickoff", date="2026-05-16",
        summary="Discussed scope.", decisions="Use OAuth2.",
        created_by="atelier-product-manager-1",
    )
    assert captured["target_table"] == "meeting_minutes"
    assert captured["librarian_output"]["domain"] == "meeting"
    assert "Discussed scope." in captured["librarian_output"]["searchable"]


def test_embedding_unavailable_is_swallowed_and_logged(fake_memex, monkeypatch):
    """When embeddings.encode raises EmbeddingUnavailable, persist with
    embedding=None AND record the skip via embeddings.log_skip (memex
    v2.4.1 contract). Untyped exceptions must propagate — see
    test_embedding_generic_exception_propagates below."""
    from scripts import embeddings as memex_embeddings

    captured_embedding = {}
    log_skip_calls = []

    def boom(text):
        raise memex_embeddings.EmbeddingUnavailable(
            reason="not_configured", provider="openai",
            detail="OPENAI_API_KEY unset",
        )

    def fake_log_skip(exc, *, caller_agent_id, index_id, input_chars):
        log_skip_calls.append({
            "reason": exc.reason, "caller_agent_id": caller_agent_id,
            "index_id": index_id, "input_chars": input_chars,
        })

    def fake_write_entry(**kwargs):
        captured_embedding["embedding"] = kwargs["embedding"]
        return {"status": "ingested",
                "index_id": kwargs["librarian_output"]["index_id"],
                "key": kwargs["librarian_output"]["key"],
                "domain": kwargs["librarian_output"]["domain"],
                "row_id": 1, "relations": []}

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_embed", boom)
    monkeypatch.setattr(backend_memex, "_memex_log_embedding_skip", fake_log_skip)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    backend_memex.write_task(
        title="x", description="y", project_id=1,
        created_by="atelier-product-manager-1",
    )
    assert captured_embedding["embedding"] is None
    assert len(log_skip_calls) == 1
    assert log_skip_calls[0]["reason"] == "not_configured"
    assert log_skip_calls[0]["caller_agent_id"] == "atelier-product-manager-1"


def test_embedding_generic_exception_propagates(fake_memex, monkeypatch):
    """Memex v2.4.1 narrowed the contract: only EmbeddingUnavailable is
    a degraded-mode signal. A generic exception means a real bug; it
    must not be silently treated as 'no embedding today.'"""
    def boom(text):
        raise RuntimeError("unexpected DB-side failure")

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_embed", boom)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    with pytest.raises(RuntimeError, match="unexpected DB-side failure"):
        backend_memex.write_task(
            title="x", description="y", project_id=1,
            created_by="atelier-product-manager-1",
        )


def test_duplicate_key_triggers_single_retry(fake_memex, monkeypatch):
    """Memex v2.3.0+ enforces UNIQUE on documents.key. On a race between
    seq-allocation and write, `librarian.write_entry` raises
    DuplicateKeyError. `_atelier_write` retries ONCE with a fresh seq."""
    # Stand up a fake librarian module so DuplicateKeyError is importable.
    import sys, types
    fake_librarian = types.ModuleType("scripts.agents.librarian")
    class _DupErr(Exception):
        def __init__(self, *, key=None, existing_index_id=None):
            self.key = key
            self.existing_index_id = existing_index_id
    fake_librarian.DuplicateKeyError = _DupErr
    sys.modules["scripts.agents.librarian"] = fake_librarian

    call_count = {"n": 0}
    def fake_write_entry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _DupErr(key=kwargs["librarian_output"]["key"])
        return {"status": "ingested",
                "index_id": kwargs["librarian_output"]["index_id"],
                "key": kwargs["librarian_output"]["key"],
                "domain": kwargs["librarian_output"]["domain"],
                "row_id": 7, "relations": []}

    seq_calls = {"n": 0}
    def fake_next_seq(*a, **k):
        seq_calls["n"] += 1
        return seq_calls["n"]  # 1, then 2 on retry

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: "myproj")
    monkeypatch.setattr(backend_memex, "_next_seq", fake_next_seq)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    result = backend_memex.write_task(
        title="Fix bug", description="x", project_id=1,
        created_by="atelier-product-manager-1",
    )
    assert call_count["n"] == 2  # retried exactly once
    assert result["row_id"] == 7
    assert result["key"].endswith("-fix-bug-2")  # second seq used


def test_canonical_key_format(fake_memex, monkeypatch):
    """Verify spec §6.7 key shape: workspace/project/domain/date-title-seq."""
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry",
        lambda **k: (captured.update(k), {"status": "ingested",
                                          "index_id": "x",
                                          "key": k["librarian_output"]["key"],
                                          "domain": "task",
                                          "row_id": 1, "relations": []})[1])
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: "auth-svc")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 3)
    monkeypatch.setattr(backend_memex, "_auto_relations",
                        lambda md, r: list(r or []))

    backend_memex.write_task(
        title="Fix OAuth Race", description="x", project_id=1,
        created_by="atelier-engineer-1",
    )
    key = captured["librarian_output"]["key"]
    # Format: atelier/auth-svc/task/YYYY-MM-DD-fix-oauth-race-3
    parts = key.split("/")
    assert parts[0] == "atelier"
    assert parts[1] == "auth-svc"
    assert parts[2] == "task"
    assert parts[3].endswith("-fix-oauth-race-3")


def test_auto_part_of_relation_when_project_id_in_metadata(fake_memex,
                                                             monkeypatch):
    """Per spec §6.9, atelier writes should auto-populate `part_of`
    edges from the new document to the owning project's document."""
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry",
        lambda **k: (captured.update(k), {"status": "ingested",
                                          "index_id": "x",
                                          "key": k["librarian_output"]["key"],
                                          "domain": "task",
                                          "row_id": 1,
                                          "relations": k["librarian_output"]["relations"]})[1])
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug",
                        lambda pid: "p")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    # Simulate one project document found for project_id=1.
    monkeypatch.setattr(backend_memex, "_auto_relations",
        lambda md, r: list(r or []) + [
            {"rel_type": "part_of", "to_index_id": "proj-idx-1"},
        ] if (md or {}).get("project_id") else list(r or []))

    backend_memex.write_task(
        title="t", description="d", project_id=1,
        created_by="atelier-engineer-1",
    )
    rels = captured["librarian_output"]["relations"]
    assert any(r["rel_type"] == "part_of" and r["to_index_id"] == "proj-idx-1"
               for r in rels)
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts import domain_vocabulary


def _memex_plugin_root() -> Path:
    """Locate the installed Memex plugin's root directory by reading the
    pinned location in ~/.memex/config.json (Memex v2.5.0+ contract).

    This replaces the older lex-sort over the Claude Code plugin cache,
    which was unstable across Memex versions (`2.10.0 < 2.2.0`) and
    fragile across plugin marketplaces.
    """
    config_path = Path.home() / ".memex" / "config.json"
    if not config_path.exists():
        raise RuntimeError(
            f"Memex config.json not found at {config_path}. Memex is not "
            "bootstrapped — run `memex:run` once to trigger Step 0.2 "
            "auto-bootstrap, or fall back to Atelier Local mode."
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Memex config.json at {config_path} is unreadable: {exc}."
        ) from exc
    plugin_root_str = data.get("plugin_root")
    if not plugin_root_str:
        raise RuntimeError(
            f"Memex config.json at {config_path} has no `plugin_root` field. "
            "Re-bootstrap memex via `memex:run`."
        )
    plugin_root = Path(plugin_root_str)
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        raise RuntimeError(
            f"Memex plugin manifest not found at {manifest}. The pinned "
            "plugin_root in ~/.memex/config.json is stale."
        )
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Memex plugin manifest at {manifest} is unreadable: {exc}."
        ) from exc
    if manifest_data.get("name") != "memex":
        raise RuntimeError(
            f"Plugin at {plugin_root} is not memex "
            f"(name={manifest_data.get('name')!r})."
        )
    return plugin_root


# Back-compat alias for legacy call sites in tests; prefer _memex_plugin_root.
_memex_plugin_dir = _memex_plugin_root


def _ensure_memex_importable() -> None:
    p = str(_memex_plugin_root())
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
    """Direct wrapper around Memex's embeddings.encode. Raises
    embeddings.EmbeddingUnavailable on provider miss — caller handles
    audit logging."""
    _ensure_memex_importable()
    from scripts import embeddings as memex_embeddings  # type: ignore
    return memex_embeddings.encode(text)


def _memex_log_embedding_skip(exc, *, caller_agent_id: str,
                              index_id: str, input_chars: int) -> None:
    """Forward to Memex's structured audit log per v2.4.1 contract."""
    _ensure_memex_importable()
    from scripts import embeddings as memex_embeddings  # type: ignore
    memex_embeddings.log_skip(
        exc,
        caller_agent_id=caller_agent_id,
        index_id=index_id,
        input_chars=input_chars,
    )


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, *, max_length: int = 64) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_length]


def _metadata_narrative(metadata: dict) -> str:
    """Join string-valued metadata fields into a single searchable blob.

    Per spec §6.8, free-text metadata fields (notes, decisions, summary,
    etc.) contribute to FTS5 ranking. We deliberately drop non-string
    values (project_id, priority) — those are filterable structured
    columns, not searchable narrative.
    """
    if not metadata:
        return ""
    parts: list[str] = []
    for value in metadata.values():
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def _build_key(*, workspace_slug: str, project_slug: str | None,
               domain: str, created_at_iso: str, title: str) -> str:
    """Canonical key per spec §6.7:
    `<workspace_slug>/<project_slug>/<domain>/<date>-<title_slug>-<seq>`.

    Memex v2.3.0+ enforces `UNIQUE` on `documents.key` (see
    `memex/db/index.sql:26`), so we allocate the smallest unused `seq`
    for the (workspace/project/domain/date/title) prefix.
    """
    date_str = created_at_iso[:10]  # YYYY-MM-DD
    title_slug = _slug(title, max_length=48)
    project_part = project_slug or "(no-project)"
    seq = _next_seq(workspace_slug, project_part, domain, date_str, title_slug)
    return f"{workspace_slug}/{project_part}/{domain}/{date_str}-{title_slug}-{seq}"


def _next_seq(workspace_slug: str, project_slug: str, domain: str,
              date_str: str, title_slug: str) -> int:
    """Smallest unused integer ≥ 1 for the (workspace/project/domain/date/title)
    prefix. Runs a `key LIKE prefix%` scan over `index.documents`."""
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    prefix = f"{workspace_slug}/{project_slug}/{domain}/{date_str}-{title_slug}-"
    existing = memex_stores.query(
        "index",
        "SELECT key FROM documents WHERE key LIKE ?",
        (prefix + "%",),
    )
    used: set[int] = set()
    for row in existing:
        suffix = row["key"][len(prefix):]
        try:
            used.add(int(suffix))
        except ValueError:
            pass
    n = 1
    while n in used:
        n += 1
    return n


def _try_embed(text: str, *, caller_agent_id: str, index_id: str) -> bytes | None:
    """Best-effort embedding.

    Narrows to embeddings.EmbeddingUnavailable per memex v2.4.1 — any
    other exception is a real bug and propagates. On the typed miss,
    forwards to memex's audit log (embeddings.log_skip) so the skip is
    visible in ~/.memex/audits/embedding-skip-log.md, then returns None
    so the write proceeds FTS5-only.
    """
    _ensure_memex_importable()
    from scripts import embeddings as memex_embeddings  # type: ignore
    try:
        return _memex_embed(text)
    except memex_embeddings.EmbeddingUnavailable as e:
        _memex_log_embedding_skip(
            e,
            caller_agent_id=caller_agent_id,
            index_id=index_id,
            input_chars=len(text),
        )
        return None


_WORKSPACE_SLUG = "atelier"  # Single-workspace deployment; spec §6.7.


def _resolve_project_slug(project_id: int | None) -> str | None:
    """Best-effort project_id → project_slug lookup for key construction.

    On miss (no project_id, or row absent) returns None and `_build_key`
    falls back to the `(no-project)` literal. Cheap query — caller is on
    the write path which is already a multi-call sequence."""
    if project_id is None:
        return None
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    try:
        rows = memex_stores.query(
            "atelier",
            "SELECT slug FROM projects WHERE id = ?",
            (project_id,),
        )
    except Exception:
        return None
    return rows[0]["slug"] if rows and rows[0].get("slug") else None


def _auto_relations(metadata: dict, explicit: list[dict]) -> list[dict]:
    """Auto-populate `part_of` edge when `project_id` is in metadata.

    Per spec §6.9, atelier writes attach `part_of` edges from their
    document to the owning project's document. Callers can still pass
    explicit relations; duplicates by `(rel_type, to_index_id)` are
    deduped here.
    """
    relations = list(explicit or [])
    project_id = (metadata or {}).get("project_id")
    if project_id is not None:
        _ensure_memex_importable()
        from scripts import stores as memex_stores  # type: ignore
        try:
            rows = memex_stores.query(
                "index",
                "SELECT index_id FROM documents WHERE domain = ? "
                "AND json_extract(metadata, '$.project_id') = ?",
                ("project", project_id),
            )
        except Exception:
            rows = []
        seen = {(r.get("rel_type"), r.get("to_index_id")) for r in relations}
        for row in rows:
            edge = ("part_of", row["index_id"])
            if edge not in seen:
                relations.append({"rel_type": "part_of",
                                  "to_index_id": row["index_id"]})
                seen.add(edge)
    return relations


def _atelier_write(*, target_table: str, domain: str, title: str,
                   body: str, payload: dict, metadata: dict,
                   relations: list[dict] | None, caller_agent_id: str) -> dict:
    """Tier 2 atelier write — synchronous, no LLM dispatch.

    Builds librarian_output deterministically, validates via Memex, and
    persists via librarian.write_entry. The target row goes into
    ~/.memex/atelier.db.<target_table> with an index_id linkback;
    the matching documents row goes into ~/.memex/index.db.

    Per spec §6.7 + §6.8:
    - `key` is `<workspace>/<project>/<domain>/<date>-<title>-<seq>` (UNIQUE)
    - `searchable` is `title + body + metadata_narrative` (no truncation cap)
    """
    domain_vocabulary.assert_valid(domain)

    created_at = _now()
    project_slug = _resolve_project_slug((metadata or {}).get("project_id"))
    key = _build_key(
        workspace_slug=_WORKSPACE_SLUG,
        project_slug=project_slug,
        domain=domain,
        created_at_iso=created_at,
        title=title,
    )
    searchable = "\n\n".join(filter(None, [
        title,
        body or "",
        _metadata_narrative(metadata or {}),
    ]))
    final_relations = _auto_relations(metadata or {}, relations or [])

    def _attempt(this_key: str) -> dict:
        output = _memex_validate_output({
            "index_id":   str(uuid.uuid4()),
            "key":        this_key,
            "domain":     domain,
            "searchable": searchable,
            "metadata":   metadata or {},
            "relations":  final_relations,
        })
        embedding = _try_embed(
            output["searchable"],
            caller_agent_id=caller_agent_id,
            index_id=output["index_id"],
        )
        return _memex_write_entry(
            payload=payload,
            librarian_output=output,
            target_store="atelier",
            target_table=target_table,
            caller_agent_id=caller_agent_id,
            embedding=embedding,
        )

    _ensure_memex_importable()
    from scripts.agents import librarian as memex_librarian  # type: ignore
    try:
        return _attempt(key)
    except memex_librarian.DuplicateKeyError:
        # Race: another writer claimed the seq we computed. Re-allocate
        # once and retry. If that still collides, surface the error.
        retry_key = _build_key(
            workspace_slug=_WORKSPACE_SLUG,
            project_slug=project_slug,
            domain=domain,
            created_at_iso=created_at,
            title=title,
        )
        return _attempt(retry_key)


# ── Document writes ────────────────────────────────────────────────────────

# Map Atelier domain → target table in ~/.memex/atelier.db.
# Per spec §6.4 the catch-all narrative domains (`design`, `research`,
# `postmortem`, `log`) land in `project_documents`. The 9 domains here
# must match `scripts.domain_vocabulary.DOMAINS` (Plan 1 Task 6 / F7).
_DOMAIN_TO_TABLE = {
    "project":     "projects",
    "task":        "tasks",
    "meeting":     "meeting_minutes",
    "project_doc": "project_documents",
    "adr":         "project_documents",
    "design":      "project_documents",
    "research":    "project_documents",
    "postmortem":  "project_documents",
    "log":         "project_documents",
}


def write_document(*, domain: str, title: str, body: str,
                   metadata: dict, caller_agent_id: str,
                   source_url: str | None = None,
                   relations: list[dict] | None = None) -> dict:
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
        metadata=metadata or {}, relations=relations,
        caller_agent_id=caller_agent_id,
    )


def write_task(*, title: str, description: str, project_id: int,
               created_by: str, assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None,
               source_ref: str | None = None,
               relations: list[dict] | None = None) -> dict:
    """`source_ref` is an optional stable origin tag (e.g.
    `"atelier:tasks:42"`); Plan 4's `migrate_to_memex.py` passes it
    positionally so a rerun can locate the row via
    `lookup_index_id_by_source_ref` (Task 3) and skip duplicate writes.
    Folded into metadata so it survives into
    `~/.memex/index.db.documents.metadata`."""
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
    # `notes` is searchable narrative — include it in metadata so
    # _metadata_narrative folds it into the FTS5 blob.
    metadata: dict = {"project_id": project_id, "priority": priority}
    if assigned_to:
        metadata["assigned_to"] = assigned_to
    if notes:
        metadata["notes"] = notes
    if source_ref:
        metadata["source_ref"] = source_ref
    return _atelier_write(
        target_table="tasks", domain="task",
        title=title, body=body, payload=payload,
        metadata=metadata, relations=relations,
        caller_agent_id=created_by,
    )


def write_meeting(*, title: str, date: str, summary: str,
                  decisions: str, created_by: str,
                  project_id: int | None = None,
                  source_ref: str | None = None,
                  relations: list[dict] | None = None) -> dict:
    """`source_ref` is an optional stable origin tag — same contract as
    `write_task`. Plan 4 line 306 passes it positionally during
    migration replay."""
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
    metadata: dict = {"date": date,
                      "summary": summary or "",
                      "decisions": decisions or ""}
    if project_id is not None:
        metadata["project_id"] = project_id
    if source_ref:
        metadata["source_ref"] = source_ref
    return _atelier_write(
        target_table="meeting_minutes", domain="meeting",
        title=title, body=body, payload=payload,
        metadata=metadata, relations=relations,
        caller_agent_id=created_by,
    )


def write_project(*, workspace_id: int, slug: str, name: str,
                  description: str, created_by: str,
                  relations: list[dict] | None = None) -> dict:
    """Create a new project — distinct facade method per user decision +
    spec §4.3. Mirrors `write_document` but pins `domain="project"` and
    targets `projects`. `slug` is the canonical project identifier used
    later by `_resolve_project_slug` for key construction."""
    payload = {
        "workspace_id": workspace_id,
        "slug": slug,
        "name": name,
        "description": description,
        "phase": "design:open",
        "created_by": created_by,
        "created_at": _now(),
        "updated_at": _now(),
    }
    metadata = {
        "workspace_id": workspace_id,
        "slug": slug,
        "description": description or "",
    }
    return _atelier_write(
        target_table="projects", domain="project",
        title=name, body=description or "", payload=payload,
        metadata=metadata, relations=relations,
        caller_agent_id=created_by,
    )
```

- [ ] **Step 4: Run tests — expect pass**

```
pytest tests/test_backend_memex_documents.py -v
```
Expected: 9 passed. (5 original + write_project + duplicate-key retry + canonical-key format + auto-part_of relation.)

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
#
# Route through memex.stores' public API rather than hand-built SQL so we
# benefit from `safe_identifier` validation and don't reintroduce
# SQL-injection risk. `insert` returns the inserted row; `update` returns
# the updated row; `query` is SELECT-only (no commit).

def _memex_core_insert(*, store: str, table: str, row: dict) -> dict:
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    return memex_stores.insert(store, table, row)


def _memex_core_update(*, store: str, table: str, row_id: int, changes: dict) -> dict:
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    updated = memex_stores.update(store, table, row_id, changes)
    return updated or {}


def _memex_core_query(*, store: str, table: str, where: dict | None = None) -> list[dict]:
    """Read-side helper. Builds a simple equality WHERE clause; column
    names are pinned to safe identifiers by callers (no user-controlled
    column names reach here)."""
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
    import sys, types
    fake_stores = types.SimpleNamespace(query=fake_query)
    monkeypatch.setitem(sys.modules, "scripts.stores", fake_stores)

    result = backend_memex.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:42")
    assert result == "01HXYZ-task-42"
    # Must target the federated index, not atelier
    assert captured["name"] == "index"
    assert "json_extract(metadata, '$.source_ref')" in captured["sql"]
    assert captured["params"] == ("atelier:tasks:42",)


def test_lookup_index_id_by_source_ref_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    import sys, types
    monkeypatch.setitem(sys.modules, "scripts.stores",
                        types.SimpleNamespace(query=lambda *a, **k: []))
    assert backend_memex.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:999") is None


def test_find_or_create_role_creates_on_miss(monkeypatch):
    """Role absent — creates and returns the new row."""
    listed = []
    created = {}

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    import sys, types
    def list_roles(db_path):
        return list(listed)
    def create_role(db_path, *, name, description):
        created["name"] = name
        created["description"] = description
        row = {"id": 7, "name": name, "description": description}
        listed.append(row)
        return row
    monkeypatch.setitem(sys.modules, "scripts.roles",
                        types.SimpleNamespace(list_roles=list_roles,
                                              create_role=create_role))

    r = backend_memex.find_or_create_role(name="Product Manager",
                                           description="PM coord")
    assert r["id"] == 7
    assert created["name"] == "Product Manager"


def test_find_or_create_role_returns_existing_on_hit(monkeypatch):
    """Role present — returns the existing row, no create call."""
    create_calls = []
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    import sys, types
    def list_roles(db_path):
        return [{"id": 3, "name": "Product Manager",
                 "description": "existing"}]
    def create_role(db_path, *, name, description):
        create_calls.append((name, description))
        return {}
    monkeypatch.setitem(sys.modules, "scripts.roles",
                        types.SimpleNamespace(list_roles=list_roles,
                                              create_role=create_role))

    r = backend_memex.find_or_create_role(name="Product Manager",
                                           description="ignored")
    assert r["id"] == 3
    assert create_calls == []


def test_find_or_create_agent_creates_on_miss(monkeypatch):
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    import sys, types
    created = {}
    def get_agent(db_path, agent_id):
        return None
    def create_agent(db_path, agent_id, name, role_id, profile):
        created.update(agent_id=agent_id, name=name,
                       role_id=role_id, profile=profile)
        return {"id": agent_id, "name": name, "role_id": role_id,
                "profile": profile}
    monkeypatch.setitem(sys.modules, "scripts.agents",
                        types.SimpleNamespace(get_agent=get_agent,
                                              create_agent=create_agent))

    r = backend_memex.find_or_create_agent(
        agent_id="atelier-pm-1", name="PM", role_id=7, profile="pm")
    assert r["id"] == "atelier-pm-1"
    assert created["role_id"] == 7


def test_find_or_create_agent_returns_existing_on_hit(monkeypatch):
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    monkeypatch.setattr(backend_memex, "_agents_db_path",
                        lambda: "/fake/agents.db")
    import sys, types
    create_calls = []
    def get_agent(db_path, agent_id):
        return {"id": agent_id, "name": "PM", "role_id": 3}
    def create_agent(*a, **k):
        create_calls.append((a, k))
        return {}
    monkeypatch.setitem(sys.modules, "scripts.agents",
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
    import sqlite3
    db = tmp_path / "fake.db"
    # Seed a tiny table
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
    conn.execute("INSERT INTO t VALUES (1, 10), (1, 20), (2, 30)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    import sys, types
    fake_registry = types.SimpleNamespace(
        get_store=lambda name: {"path": str(db)} if name == "atelier" else None
    )
    monkeypatch.setitem(sys.modules, "scripts.registry", fake_registry)
    # Memex's get_connection sets pragmas; we stub with stdlib sqlite3
    # since the fake.db doesn't need them.
    fake_db = types.SimpleNamespace(get_connection=lambda p: sqlite3.connect(p))
    monkeypatch.setitem(sys.modules, "scripts.db", fake_db)

    n = backend_memex._memex_core_execute(
        store="atelier", sql="DELETE FROM t WHERE a = ?", params=(1,))
    assert n == 2


def test_memex_core_execute_no_match_returns_zero(monkeypatch, tmp_path):
    import sqlite3
    db = tmp_path / "fake.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)
    import sys, types
    monkeypatch.setitem(sys.modules, "scripts.registry",
                        types.SimpleNamespace(
                            get_store=lambda n: {"path": str(db)}))
    monkeypatch.setitem(sys.modules, "scripts.db",
                        types.SimpleNamespace(
                            get_connection=lambda p: sqlite3.connect(p)))

    n = backend_memex._memex_core_execute(
        store="atelier", sql="DELETE FROM t WHERE id = ?", params=(999,))
    assert n == 0
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
    """Run an FTS5-only Memex Index search by calling the Reference
    Librarian's `execute_query_plan` directly. We skip the subagent step
    (`ask_prepare` builds an LLM prompt we'd never dispatch), so the
    prep helper is dead code on this path. Brain-style ask/synthesize
    (with the subagent loop) still go via `memex:run`.
    """
    _ensure_memex_importable()
    from scripts.agents import reference_librarian as memex_ref  # type: ignore
    plan = {"fts_query": query, "vector_query": None,
            "filters": {}, "limit": limit}
    if domain:
        plan["filters"]["domain"] = domain
    if project_id is not None:
        # FTS5 metadata filter on JSON-extracted project_id.
        plan["filters"]["project_id"] = project_id
    return memex_ref.execute_query_plan(plan, with_embedding=False)


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


# ── Cross-plan helpers (added per cross-plan dependency audit) ─────────────

def lookup_index_id_by_source_ref(*, source_ref: str) -> str | None:
    """Look up the `index_id` of a previously-written document whose
    `metadata.source_ref` equals `source_ref`. Returns None if absent.

    Plan 4 contract: `migrate_to_memex.py` calls this before every
    replay write so a rerun after a partial outage skips rows that
    already landed in Memex (avoiding `librarian.DuplicateKeyError`).
    Source refs are stable strings like `"atelier:tasks:42"`.
    """
    _ensure_memex_importable()
    from scripts import stores as memex_stores  # type: ignore
    rows = memex_stores.query(
        "index",
        "SELECT index_id FROM documents "
        "WHERE json_extract(metadata, '$.source_ref') = ? LIMIT 1",
        (source_ref,),
    )
    return rows[0]["index_id"] if rows else None


def _agents_db_path() -> str:
    """Resolve the path to `~/.memex/agents.db` via the public registry.

    Memex's `scripts.registry.get_store("agents")` returns the
    registered store record (which carries `path`). `agents` is a
    reserved store name seeded by Memex bootstrap.
    """
    _ensure_memex_importable()
    from scripts import registry as memex_registry  # type: ignore
    rec = memex_registry.get_store("agents")
    if rec is None:
        raise RuntimeError(
            "Memex has no 'agents' store registered. Run `memex:run` once "
            "to bootstrap before calling Atelier role/agent helpers."
        )
    return rec["path"]


def find_or_create_role(*, name: str, description: str) -> dict:
    """Return the role row with this `name`, creating it if absent.

    Idempotent — safe to call against a populated agents.db. Used by
    Plan 3's `scripts/seed_roles.py` rewire to re-seed canonical roles
    without `IntegrityError` on already-present names.
    """
    _ensure_memex_importable()
    from scripts import roles as memex_roles  # type: ignore
    db_path = _agents_db_path()
    for r in memex_roles.list_roles(db_path):
        if r["name"] == name:
            return r
    return memex_roles.create_role(db_path, name=name, description=description)


def find_or_create_agent(*, agent_id: str, name: str, role_id: int,
                         profile: str) -> dict:
    """Return the agent row with this `agent_id`, creating it if absent.

    Idempotent — symmetric to `find_or_create_role`. Memex's
    `scripts.agents.create_agent` signature is
    `(db_path, agent_id, name, role_id, profile)` per
    `memex/scripts/agents/__init__.py:26`.
    """
    _ensure_memex_importable()
    from scripts import agents as memex_agents  # type: ignore
    db_path = _agents_db_path()
    existing = memex_agents.get_agent(db_path, agent_id)
    if existing is not None:
        return existing
    return memex_agents.create_agent(db_path, agent_id, name, role_id, profile)


def _memex_core_execute(*, store: str, sql: str,
                        params: tuple = ()) -> int:
    """Composite-key / non-equality DELETE / UPDATE primitive.

    `memex_stores.query()` is SELECT-only (no commit);
    `memex_stores.delete()` only handles integer-PK rows. Plan 3's
    `scripts/meetings.py` rewrite needs to clear `meeting_participants`
    rows by composite `(meeting_id, agent_id)` — neither helper covers
    this case, so we open the underlying connection directly via the
    public registry record and `scripts.db.get_connection`.

    Returns affected `rowcount`. Caller passes hand-built SQL; this
    helper does NOT validate identifiers (the SQL string is wholly
    inside Atelier's source — no user-controlled fragments reach
    here). Restrict use to DELETE / UPDATE statements that the other
    `_memex_core_*` helpers can't express.
    """
    _ensure_memex_importable()
    from scripts import registry as memex_registry  # type: ignore
    from scripts.db import get_connection  # type: ignore
    rec = memex_registry.get_store(store)
    if rec is None:
        raise ValueError(f"Unknown store: {store}")
    conn = get_connection(rec["path"])
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests — expect pass**

```
pytest tests/test_backend_memex_reads.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_memex.py tests/test_backend_memex_reads.py
git commit -m "feat(backend-memex): wave-1 reads + cross-plan helpers (source_ref lookup, idempotent role/agent, raw execute)"
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
    # Bootstrap calls memex's public role/agent/store CRUD directly.
    assert "create_role" in text
    assert "create_agent" in text
    assert "create_store" in text
    # And precondition-checks memex's own bootstrap state.
    assert "require_bootstrap" in text
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
the Librarian subagent must be dispatched via the Task tool. The
corresponding internal procedure (`internal/memex/dispatch-ingest`)
is **deferred to a future plan** — currently atelier callers that
need Tier 3 should invoke `memex:run` directly with an `ingest`
intent until the procedure exists.

## Inputs

- `domain` — must be in `scripts.domain_vocabulary.DOMAINS`
- `title`, `body` — `body` is searchable narrative; `searchable` is composed per spec §6.8 as `title + "\n\n" + body + "\n\n" + metadata_narrative(metadata)` (no truncation cap).
- `payload` — dict of target-table columns (will be persisted in `~/.memex/atelier.db.<table>`)
- `target_table` — one of `projects`, `tasks`, `meeting_minutes`, `project_documents`
- `caller_agent_id` — an Atelier-seeded agent (`atelier-product-manager-1`, etc.)
- `metadata` — optional dict written to `index.db.documents.metadata`; string-valued entries fold into `searchable`
- `relations` — optional list of `{"to_index_id": ..., "rel_type": ...}` for explicit graph edges. The recipe also auto-attaches a `part_of` edge to the owning project's document when `metadata["project_id"]` is set (spec §6.9).

## Recipe

The procedure body is `scripts.backend_memex._atelier_write(...)`. It:

1. Calls `domain_vocabulary.assert_valid(domain)` — rejects unknown domains.
2. Resolves `project_slug` from `metadata["project_id"]` and constructs the canonical key per spec §6.7: `<workspace>/<project>/<domain>/<YYYY-MM-DD>-<title_slug>-<seq>`. `seq` is the smallest unused integer ≥ 1 across existing keys with the same prefix (Memex v2.3.0+ enforces UNIQUE on `documents.key`).
3. Composes `searchable` per spec §6.8 (no truncation cap).
4. Builds the classification dict (`index_id` = `uuid.uuid4()`, canonical `key`, `domain`, `searchable`, `metadata`, `relations`) and runs it through `librarian.validate_output()` — the shared schema check Memex v2.2.0 exposes.
5. Best-effort embedding via `embeddings.encode(searchable)`. Narrow the catch to `embeddings.EmbeddingUnavailable` (memex v2.4.1 contract) — any other exception is a real bug and propagates. On the typed miss, call `embeddings.log_skip(e, caller_agent_id, index_id, input_chars)` so the skip is visible in `~/.memex/audits/embedding-skip-log.md`, then carry `None` forward.
6. Calls `librarian.write_entry(payload, librarian_output, target_store="atelier", target_table, caller_agent_id, embedding)` — Memex's canonical two-stage write (Index row → target-store row → row_id backlink). On `DuplicateKeyError` (race on `seq`), bumps `seq` and retries once; further collisions propagate.
7. Returns `{"status": "ingested", "index_id", "key", "domain", "row_id", "relations"}`.

## Errors

- `RuntimeError: Memex plugin not found` — Memex isn't installed despite `mode_detector` returning `memex`. Recover: re-run `mode_detector._clear_cache()` and re-detect; fall back to Local.
- `ValueError: unknown domain` — caller passed a domain outside `DOMAINS`. Use one of the vocabulary entries or amend the spec (see `internal/memex/domain-vocabulary.md`).
- `ValueError: Unknown store: atelier` — bootstrap has not run. Caller must run `internal/bootstrap-memex/SKILL.md` first.
- `ValueError: librarian_output missing fields` — shouldn't happen since `_atelier_write` builds the dict; if seen, it indicates a Memex schema bump. Pin the Memex version requirement and update Atelier.

## `source_ref` contract for idempotent replay

Callers may set `metadata["source_ref"]` to a stable string identifying
the row's origin (e.g. `"atelier:tasks:42"` from Plan 4's
`scripts/migrate_to_memex.py`). Because `_atelier_write` passes
`metadata` through verbatim into the validated `librarian_output` dict
(see `scripts/backend_memex.py:_atelier_write` lines ~700-770), the
`source_ref` lands in `~/.memex/index.db.documents.metadata` as
JSON-extractable narrative — no code change needed in the write path.

The matching reverse-lookup method
`backend_memex.lookup_index_id_by_source_ref(source_ref)` (Task 3 below,
also surfaced through `scripts/backend.py`) lets callers query
`SELECT index_id FROM documents WHERE json_extract(metadata,
'$.source_ref') = ?` before deciding whether to re-emit. This is what
makes Plan 4's `migrate_project` safe to rerun after a partial outage:
each `atelier:<table>:<local_id>` row is checked against the Index
first, and already-replayed rows are counted under `already_present`
instead of triggering `librarian.DuplicateKeyError`.

`write_task` and `write_meeting` accept `source_ref` as a top-level
kwarg (added in Task 1 below) and fold it into their internal
`metadata` dict — the migrator passes it positionally per Plan 4 lines
293, 306. `write_document` and `write_project` rely on the caller to
include it in `metadata` directly (Plan 4 line 278).
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
from scripts import backend_memex
import sys, json
from pathlib import Path

# 1. Make sure Memex is reachable.
assert mode_detector.detect_mode() == "memex"

# 2. Precondition: Memex itself must be bootstrapped (registry.json +
#    config.json present). If not, fail with operator guidance — Atelier
#    can't seed on top of an uninitialized Memex.
backend_memex._ensure_memex_importable()
from scripts import db as memex_db  # type: ignore  # noqa: E402
try:
    memex_db.require_bootstrap()
except memex_db.MemexNotInitializedError:
    raise RuntimeError(
        "Memex is not bootstrapped. Run `memex:run` once to trigger Step 0.2 "
        "auto-bootstrap, then re-run atelier's bootstrap."
    )

# 3. Import Memex's CRUD modules (plugin_root resolved via config.json).
from scripts import roles as memex_roles, agents as memex_agents, stores as memex_stores  # type: ignore
from scripts import registry as memex_registry  # type: ignore
memex_home_path = memex_db.memex_home()
agents_db = str(memex_home_path / "agents.db")

# 4. Seed roles (idempotent — guard against IntegrityError).
import sqlite3 as _sqlite3
for r in seed_data.load_role_seed():
    existing = [x for x in memex_roles.list_roles(agents_db) if x["name"] == r["name"]]
    if existing:
        continue
    try:
        memex_roles.create_role(agents_db, name=r["name"], description=r["description"])
    except _sqlite3.IntegrityError:
        pass  # Race: another writer seeded the same role; ignore.

# 5. Seed agents (idempotent — guard against IntegrityError).
#    Iterates the FULL templates/agents/ directory (~50 personas) — not
#    a fixed list. seed_data.load_agent_seed() returns one record per file.
role_map = {r["name"]: r["id"] for r in memex_roles.list_roles(agents_db)}
for a in seed_data.load_agent_seed():
    if memex_agents.get_agent(agents_db, a["agent_id"]) is not None:
        continue
    try:
        memex_agents.create_agent(agents_db, a["agent_id"], a["name"],
                                  role_map[a["role_name"]], a["profile"])
    except _sqlite3.IntegrityError:
        pass

# 6. Create atelier store if absent. Use the public API (registry.json
#    is a flat map — `get_store` does the right thing).
if memex_registry.get_store("atelier") is None:
    atelier_plugin = Path(__file__).resolve().parents[2]  # plugin root
    atelier_db_path = str(memex_home_path / "atelier.db")
    memex_stores.create_store(
        name="atelier",
        path=atelier_db_path,
        migrations_dir=str(atelier_plugin / "migrations" / "shared"),
    )

# 7. Write the marker.
import datetime
marker = memex_home_path / "atelier.bootstrap.json"
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

- Roles: pre-checked via `list_roles`; raw `create_role` call is wrapped
  in `try / except sqlite3.IntegrityError` so a race-condition duplicate
  is swallowed (memex's `roles.create_role` raises IntegrityError on
  collision — there is no silent no-op).
- Agents: skipped explicitly when `get_agent` returns non-None; same
  IntegrityError guard for races.
- Store: skipped if `memex_registry.get_store("atelier")` returns
  non-None (registry.json is a flat map of `{name: record, ...}`).
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
    r = create_role(str(db), name="Product Manager", description="PM")
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


def test_write_project_creates_project_row(project_root):
    """`write_project` is a distinct facade method (user decision +
    spec §4.3). Must create a `projects` row AND a corresponding
    `documents` row for FTS5 search."""
    r = backend_local.write_project(
        workspace_id=1, slug="payments", name="Payments Service",
        description="Stripe + ACH integration.",
        created_by="atelier-product-manager-1",
    )
    assert r["domain"] == "project"
    assert r["row_id"] >= 1
    # Verify FTS5 indexes the description.
    hits = backend_local.find_documents(query="Stripe")
    assert any("Payments Service" in d.get("title", "") for d in hits)
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
    # Per spec §6.8: no truncation cap. Match the Memex-mode composition
    # so FTS5 results are consistent across modes.
    md_parts = [v for v in (metadata or {}).values()
                if isinstance(v, str) and v.strip()]
    searchable = "\n\n".join(filter(None, [title, body or "", *md_parts]))
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
               priority: int = 0, notes: str | None = None,
               source_ref: str | None = None) -> dict:
    """`source_ref` matches the memex-backend signature so callers that
    swap backends (or Plan 4's migrator on Memex mode) don't drift —
    Local mode currently ignores it (no Index to dedupe against), but
    the kwarg must exist for signature parity."""
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
    doc_metadata = {"project_id": project_id, "task_id": row_id}
    if source_ref:
        doc_metadata["source_ref"] = source_ref
    write_document(domain="task", title=title, body=body,
                   metadata=doc_metadata,
                   caller_agent_id=created_by)
    c.close()
    return {"status": "ingested", "index_id": None,
            "row_id": row_id, "key": _slug(title), "domain": "task",
            "relations": []}


def write_meeting(*, title: str, date: str, summary: str,
                  decisions: str, created_by: str,
                  project_id: int | None = None,
                  source_ref: str | None = None) -> dict:
    """`source_ref` parity with the memex-backend signature; same notes
    as `write_task` apply."""
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
    doc_metadata = {"meeting_id": row_id, "project_id": project_id}
    if source_ref:
        doc_metadata["source_ref"] = source_ref
    write_document(domain="meeting", title=title, body=body,
                   metadata=doc_metadata,
                   caller_agent_id=created_by)
    c.close()
    return {"status": "ingested", "index_id": None,
            "row_id": row_id, "key": _slug(title), "domain": "meeting",
            "relations": []}


def write_project(*, workspace_id: int, slug: str, name: str,
                  description: str, created_by: str,
                  relations: list[dict] | None = None) -> dict:
    """Create a new project in local mode. Distinct from `write_document`
    per spec §4.3 + user decision — mirrors the Memex-backend signature
    so callers can swap backends transparently."""
    c = _conn()
    cur = c.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'design:open', ?, ?, ?) RETURNING id",
        (workspace_id, slug, name, description, created_by, _now(), _now()),
    )
    row_id = cur.fetchone()[0]
    c.commit()
    c.close()
    # Index the project description so it shows up in find_documents.
    write_document(domain="project", title=name, body=description or "",
                   metadata={"project_id": row_id, "slug": slug,
                             "workspace_id": workspace_id},
                   caller_agent_id=created_by)
    return {"status": "ingested", "index_id": None,
            "row_id": row_id, "key": slug, "domain": "project",
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
    r = create_role(str(db), name="Product Manager", description="PM")
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
    r = create_role(str(db), name="Product Manager", description="PM")
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


# ── Cross-plan helpers (added per cross-plan dependency audit) ─────────────

def lookup_index_id_by_source_ref(*, source_ref: str) -> str | None:
    """Local-mode equivalent of the Memex-backend method.

    Local mode has no federated `index.db`; `documents.metadata` is a
    JSON column on the project-local SQLite table. Plan 4's
    `migrate_to_memex.py` runs in Memex mode (it's the *target* of the
    migration), so this is exercised mainly through facade-dispatch
    tests in Local mode. Returns None for every input that doesn't
    match — Local rows are usually keyed by `(project_id, slug)`, not
    `source_ref`, so most callers see None.
    """
    import json as _json
    c = _conn()
    _ensure_documents_table(c)
    rows = c.execute(
        "SELECT id, metadata FROM documents "
        "WHERE json_extract(metadata, '$.source_ref') = ? LIMIT 1",
        (source_ref,),
    ).fetchall()
    c.close()
    if not rows:
        return None
    # Local mode has no opaque index_id; surface the integer row id
    # as a string for cross-mode symmetry with the Memex backend's
    # `str` return type.
    return str(rows[0]["id"])


def find_or_create_role(*, name: str, description: str) -> dict:
    """Local-mode idempotent role helper — talks to the project-local
    `roles` table (seeded by `migrations/local-only/050_local_roles_agents.sql`
    per Plan 1 Task 5)."""
    c = _conn()
    row = c.execute("SELECT * FROM roles WHERE name = ?",
                    (name,)).fetchone()
    if row is not None:
        c.close()
        return dict(row)
    cur = c.execute(
        "INSERT INTO roles (name, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?) RETURNING *",
        (name, description, _now(), _now()),
    )
    new_row = dict(cur.fetchone())
    c.commit()
    c.close()
    return new_row


def find_or_create_agent(*, agent_id: str, name: str, role_id: int,
                         profile: str) -> dict:
    """Local-mode idempotent agent helper — talks to the project-local
    `agents` table. Signature mirrors the Memex backend so the facade
    dispatcher routes either way."""
    c = _conn()
    row = c.execute("SELECT * FROM agents WHERE id = ?",
                    (agent_id,)).fetchone()
    if row is not None:
        c.close()
        return dict(row)
    cur = c.execute(
        "INSERT INTO agents (id, name, role_id, profile, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
        (agent_id, name, role_id, profile, _now(), _now()),
    )
    new_row = dict(cur.fetchone())
    c.commit()
    c.close()
    return new_row
```

Append to `tests/test_backend_local_reads.py`:

```python
# ── Cross-plan helpers (Plan 1 facade dep) ─────────────────────────────

def test_local_lookup_index_id_by_source_ref_hit(project_root):
    # write_document carries metadata through to documents.metadata.
    backend_local.write_document(
        domain="design", title="Migrated doc", body="body",
        metadata={"project_id": 1, "source_ref": "atelier:tasks:42"},
        caller_agent_id="atelier-pm-1",
    )
    assert backend_local.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:42") is not None


def test_local_lookup_index_id_by_source_ref_miss(project_root):
    assert backend_local.lookup_index_id_by_source_ref(
        source_ref="atelier:tasks:999") is None


def test_local_find_or_create_role_creates_on_miss(project_root):
    r = backend_local.find_or_create_role(
        name="Designer", description="UI/UX")
    assert r["name"] == "Designer"
    assert r["id"] >= 1


def test_local_find_or_create_role_returns_existing_on_hit(project_root):
    first = backend_local.find_or_create_role(
        name="Designer", description="first")
    second = backend_local.find_or_create_role(
        name="Designer", description="ignored on hit")
    assert second["id"] == first["id"]


def test_local_find_or_create_agent_creates_on_miss(project_root):
    # Reuse the seeded PM role
    role = backend_local.find_or_create_role(
        name="Product Manager", description="PM")
    r = backend_local.find_or_create_agent(
        agent_id="atelier-pm-2", name="PM 2",
        role_id=role["id"], profile="pm")
    assert r["id"] == "atelier-pm-2"


def test_local_find_or_create_agent_returns_existing_on_hit(project_root):
    role = backend_local.find_or_create_role(
        name="Product Manager", description="PM")
    a = backend_local.find_or_create_agent(
        agent_id="atelier-pm-3", name="PM 3",
        role_id=role["id"], profile="pm")
    b = backend_local.find_or_create_agent(
        agent_id="atelier-pm-3", name="ignored",
        role_id=role["id"], profile="ignored")
    assert b["id"] == a["id"]
    assert b["name"] == "PM 3"  # unchanged
```

- [ ] **Step 4: Run tests — expect pass**

- [ ] **Step 5: Commit**

```bash
git add scripts/backend_local.py tests/test_backend_local_reads.py
git commit -m "feat(backend-local): wave-1' reads + cross-plan helpers (idempotent role/agent, source_ref lookup)"
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
            write_project=lambda **k: {"ok": 1},
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
            lookup_index_id_by_source_ref=lambda **k: None,
            find_or_create_role=lambda **k: {"id": 1, "name": k["name"]},
            find_or_create_agent=lambda **k: {"id": k["agent_id"]},
        ):
            backend.write_document(domain="d", title="t", body="b",
                                    metadata={}, caller_agent_id="a")
            backend.write_project(workspace_id=1, slug="p", name="P",
                                   description="d", created_by="a")
            backend.write_task(title="t", description="d", project_id=1,
                                created_by="a")
            backend.find_documents(query="q")
            assert backend.get_task(task_id=1) is None
            assert backend.list_tasks(project_id=1) == []
            assert backend.lookup_index_id_by_source_ref(
                source_ref="atelier:tasks:1") is None
            assert backend.find_or_create_role(
                name="PM", description="d")["name"] == "PM"
            assert backend.find_or_create_agent(
                agent_id="x", name="X", role_id=1,
                profile="p")["id"] == "x"
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


def write_project(**kwargs) -> dict:
    return _impl().write_project(**kwargs)


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


# Cross-plan helpers (Plan 1 facade dep). Each is symmetric across
# backends; the local-mode impl talks to the project-local SQLite,
# the memex-mode impl reads the federated index / agents.db.

def lookup_index_id_by_source_ref(**kwargs) -> str | None:
    return _impl().lookup_index_id_by_source_ref(**kwargs)


def find_or_create_role(**kwargs) -> dict:
    return _impl().find_or_create_role(**kwargs)


def find_or_create_agent(**kwargs) -> dict:
    return _impl().find_or_create_agent(**kwargs)
```

Note: `_memex_core_execute` is NOT exposed through `scripts/backend.py`. Plan 3's `scripts/meetings.py` imports `backend_memex` directly (mode-gated) for the composite-key DELETE path — that's a deliberate escape hatch because the facade only models domain-shaped operations, not raw SQL execution. The Local backend has no equivalent because Plan 3 already opens a project-local connection via `backend_local._conn()` for the same DELETE.

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

Stands up a fake Memex install in a tmp dir and writes a
`~/.memex/config.json` pointing at it so backend_memex's
`_memex_plugin_root()` resolves to the tmp plugin (no more lex-sort
of the Claude Code plugin cache). Then runs bootstrap and asserts
roles/agents/store all land correctly.
"""
import json
import shutil
import sys
import sqlite3
from pathlib import Path
import pytest


@pytest.fixture
def fake_memex_install(tmp_path, monkeypatch):
    """Create a minimal Memex plugin tree the backend can import.

    Uses MEMEX_HOME env var (Memex v2.5.0+ honors it ahead of $HOME path
    validation) rather than monkeypatching Path.home — `memex_home()`
    validates that the resolved path is under $HOME unless this env var
    is set explicitly.
    """
    plugin_root = tmp_path / "memex_plugin"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "memex", "version": "2.2.0",
    }))
    # Real Memex's scripts/ are required; copy from the actual install
    real_memex_candidates = [
        Path.home() / "apps" / "memex",
        Path.home() / "Documents" / "Skills" / "memex",
    ]
    real_memex = next((p for p in real_memex_candidates if p.exists()), None)
    if real_memex is None:
        pytest.skip("real Memex repo not available")
    shutil.copytree(real_memex / "scripts", plugin_root / "scripts")

    # ~/.memex layout: registry.json (flat map, NOT {"stores": {...}})
    # + config.json (pins the plugin_root that backend_memex reads).
    memex_home_dir = tmp_path / ".memex"
    memex_home_dir.mkdir()
    (memex_home_dir / "registry.json").write_text("{}")
    (memex_home_dir / "config.json").write_text(json.dumps({
        "plugin_root": str(plugin_root),
    }))

    # Memex v2.5.0's memex_home() validates the path. Set MEMEX_HOME
    # explicitly so it works even when tmp_path is not under $HOME.
    monkeypatch.setenv("MEMEX_HOME", str(memex_home_dir))
    return memex_home_dir.parent  # return the tmp_path root for test asserts


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
    # "Product Manager" is the canonical PM role name (user decision).
    assert {"Product Manager", "Software Engineer"} <= role_names
    assert memex_agents.get_agent(agents_db, "atelier-product-manager-1") is not None
    # registry.json is a flat map per memex/scripts/registry.py.
    registry = json.loads((fake_memex_install / ".memex" / "registry.json").read_text())
    assert "atelier" in registry  # flat map; no nested "stores" key


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
    caller-built librarian_output contract isn't there.

    The version check reads from `~/.memex/config.json`'s pinned
    plugin_root + that plugin's manifest (Plan 1 F2 / F1 contract) —
    NOT by lex-sorting the Claude Code plugin cache.
    """
    # Rewrite the fake plugin's manifest to claim v2.1.0.
    memex_home_dir = tmp_path / ".memex"
    config = json.loads((memex_home_dir / "config.json").read_text())
    plugin_root = Path(config["plugin_root"])
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "memex", "version": "2.1.0"}))

    from scripts.bootstrap import run_bootstrap
    with pytest.raises(RuntimeError, match="requires Memex v2.2.0"):
        run_bootstrap()


def test_bootstrap_fails_when_memex_not_initialized(tmp_path, monkeypatch):
    """If Memex itself isn't bootstrapped (no registry.json), atelier
    bootstrap must fail fast with operator guidance — not partway
    through with a confusing sqlite or file-missing error."""
    # No registry.json — only a config.json pointing at a manifest.
    memex_home_dir = tmp_path / ".memex"
    memex_home_dir.mkdir()
    plugin_root = tmp_path / "plugin"
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "memex", "version": "2.5.1",
    }))
    # Need real memex scripts/ for the import.
    real_memex_candidates = [
        Path.home() / "apps" / "memex",
        Path.home() / "Documents" / "Skills" / "memex",
    ]
    real_memex = next((p for p in real_memex_candidates if p.exists()), None)
    if real_memex is None:
        pytest.skip("real Memex repo not available")
    shutil.copytree(real_memex / "scripts", plugin_root / "scripts")
    (memex_home_dir / "config.json").write_text(json.dumps({
        "plugin_root": str(plugin_root),
    }))
    monkeypatch.setenv("MEMEX_HOME", str(memex_home_dir))

    from scripts.bootstrap import run_bootstrap
    with pytest.raises(RuntimeError, match="Memex is not bootstrapped"):
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
import sqlite3
import sys
from pathlib import Path
from scripts import seed_data
from scripts import backend_memex


MIN_MEMEX_VERSION = (2, 2, 0)


def _require_memex_version() -> str:
    """Atelier Tier 2 writes require Memex v2.2.0+ (caller-built
    librarian_output + librarian.validate_output). Raise if older.

    Reads the version from the plugin manifest at the path pinned in
    `~/.memex/config.json` (resolved by `backend_memex._memex_plugin_root`),
    NOT by lex-sorting the Claude Code plugin cache (that ordering breaks
    on `2.10.0 < 2.2.0`)."""
    plugin_root = backend_memex._memex_plugin_root()
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
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

    # Precondition: Memex itself must be bootstrapped. Atelier seeds on
    # top of Memex's roles/agents/registry — without those, every call
    # below would fail mid-sequence. Fail fast with operator guidance.
    from scripts import db as memex_db  # type: ignore
    try:
        memex_db.require_bootstrap()
    except memex_db.MemexNotInitializedError:
        raise RuntimeError(
            "Memex is not bootstrapped. Run `memex:run` once to trigger Step "
            "0.2 auto-bootstrap, then re-run atelier's bootstrap."
        )

    from scripts import roles as memex_roles, agents as memex_agents, stores as memex_stores  # type: ignore
    from scripts import registry as memex_registry  # type: ignore

    memex_home = memex_db.memex_home()
    agents_db = str(memex_home / "agents.db")

    # Roles: pre-check by name, then try/except IntegrityError on race.
    # `create_role` raises sqlite3.IntegrityError (NOT silent no-op).
    role_map: dict[str, int] = {}
    for r in seed_data.load_role_seed():
        existing = [x for x in memex_roles.list_roles(agents_db) if x["name"] == r["name"]]
        if existing:
            role_map[r["name"]] = existing[0]["id"]
            continue
        try:
            new = memex_roles.create_role(agents_db, name=r["name"], description=r["description"])
            role_map[r["name"]] = new["id"]
        except sqlite3.IntegrityError:
            # Race: another writer seeded this role. Refresh and continue.
            again = [x for x in memex_roles.list_roles(agents_db) if x["name"] == r["name"]]
            if again:
                role_map[r["name"]] = again[0]["id"]

    # Agents: iterates the FULL templates/agents/ directory (~50 personas)
    # via seed_data.load_agent_seed(), not a fixed 6-file list.
    for a in seed_data.load_agent_seed():
        if memex_agents.get_agent(agents_db, a["agent_id"]) is not None:
            continue
        try:
            memex_agents.create_agent(agents_db, a["agent_id"], a["name"],
                                      role_map[a["role_name"]], a["profile"])
        except sqlite3.IntegrityError:
            pass  # Race: agent already exists.

    # Store: use the public registry API (registry.json is a flat map,
    # NOT `{"stores": {...}}`). `create_store` requires an explicit `path`.
    if memex_registry.get_store("atelier") is None:
        atelier_plugin = Path(__file__).resolve().parents[1]
        atelier_db_path = str(memex_home / "atelier.db")
        memex_stores.create_store(
            name="atelier",
            path=atelier_db_path,
            migrations_dir=str(atelier_plugin / "migrations" / "shared"),
        )

    # Marker. The fallback string should match Plan 4 Task 7's bumped
    # version (currently "1.1.0-dev" — see Plan 4 Task 7 for harmonization).
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
- `scripts/backend_memex.py` and `scripts/backend_local.py` both export the same 15 names: `write_document`, `write_project`, `write_task`, `write_meeting`, `upsert_session`, `transition_phase`, `update_task_status`, `record_phase_bypass`, `find_documents`, `get_task`, `list_tasks`, `find_project_by_key`, `lookup_index_id_by_source_ref`, `find_or_create_role`, `find_or_create_agent`.
- `backend_memex.py` additionally exports `_memex_core_execute` (the composite-key DELETE primitive Plan 3's `scripts/meetings.py` calls directly; intentionally NOT routed through `scripts/backend.py` because it takes raw SQL).
- `write_task` and `write_meeting` accept optional `source_ref: str` kwarg in BOTH backends. In Memex mode it folds into `metadata["source_ref"]` so the row is discoverable by `lookup_index_id_by_source_ref` (idempotent-replay contract for Plan 4 `migrate_to_memex.py`). In Local mode the kwarg exists for signature parity but the value is effectively unused (no federated Index to dedupe against).
- `scripts/bootstrap.py` is idempotent (verified by e2e test).
- Memex writes use canonical keys per spec §6.7 (UNIQUE-safe, retried-once on DuplicateKeyError) and uncapped `searchable` per spec §6.8.
- 7 new internal/* procedures present, none surface as slash commands.
