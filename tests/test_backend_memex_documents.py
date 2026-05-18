# tests/test_backend_memex_documents.py
"""Tests for Plan 2 Task 1 — Memex-mode document writes (Tier 2).

Verifies write_document / write_project / write_task / write_meeting build
the librarian_output dict deterministically (no LLM dispatch), route to
the correct target table, handle embedding failures per memex v2.4.1
contract, and retry on DuplicateKeyError per spec §6.4.
"""
import json as _json
import sys
import types
from datetime import datetime as _datetime, timezone as _tz

import pytest

from scripts import backend_memex


@pytest.fixture
def fake_memex(tmp_path, monkeypatch):
    """Stand up a temp ~/.memex/ pointed at a temp atelier.db.

    The Memex-side modules (librarian, embeddings) are patched per test
    via monkeypatch.setattr on backend_memex's thin wrappers, so we
    don't need a real Memex install here.

    Note: registry.json is a flat map per memex/scripts/registry.py; the
    top-level keys ARE the store names (no `{"stores": {...}}` wrapper).
    """
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
    # The explicit env var (with ALLOW_UNUSUAL) is the most reliable way
    # to point at a tmp path during tests.
    monkeypatch.setenv("MEMEX_HOME", str(memex_home))
    monkeypatch.setenv("MEMEX_HOME_ALLOW_UNUSUAL", "1")
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
    # Stand up a fake `scripts.embeddings` module so backend_memex's
    # `from scripts import embeddings as memex_embeddings` resolves
    # without a real Memex install. `_try_embed` references
    # `memex_embeddings.EmbeddingUnavailable` for the `except` filter,
    # so we must inject the same class the production code catches.
    class _FakeEmbeddingUnavailable(Exception):
        def __init__(self, reason: str, provider: str, detail: str = ""):
            self.reason = reason
            self.provider = provider
            self.detail = detail
            super().__init__(f"embedding unavailable: {reason}")

    fake_embeddings = types.ModuleType("scripts.embeddings")
    fake_embeddings.EmbeddingUnavailable = _FakeEmbeddingUnavailable
    fake_embeddings.encode = lambda text: b""
    fake_embeddings.log_skip = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "scripts.embeddings", fake_embeddings)
    # Stub the importer so it doesn't try to locate ~/.memex/config.json.
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)

    captured_embedding = {}
    log_skip_calls = []

    def boom(text):
        raise _FakeEmbeddingUnavailable(
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
    # Inject a fake embeddings module so the `except` clause can
    # reference EmbeddingUnavailable without a real Memex install.
    class _FakeEmbeddingUnavailable(Exception):
        pass
    fake_embeddings = types.ModuleType("scripts.embeddings")
    fake_embeddings.EmbeddingUnavailable = _FakeEmbeddingUnavailable
    fake_embeddings.encode = lambda text: b""
    fake_embeddings.log_skip = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "scripts.embeddings", fake_embeddings)
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)

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
    # Mirror memex's actual class shape: __init__(self, key, existing_index_id).
    fake_librarian = types.ModuleType("scripts.agents.librarian")

    class _DupErr(Exception):
        def __init__(self, *args, key=None, existing_index_id=None):
            # Accept both positional (memex's real signature) and kwargs
            # forms so this stub matches whichever the production code
            # constructs against.
            if args:
                self.key = args[0]
                self.existing_index_id = args[1] if len(args) > 1 else None
            else:
                self.key = key
                self.existing_index_id = existing_index_id
            super().__init__(f"duplicate {self.key}")

    fake_librarian.DuplicateKeyError = _DupErr
    # Also need a real-shaped scripts.agents package so the
    # `from scripts.agents import librarian` import resolves.
    fake_agents_pkg = types.ModuleType("scripts.agents")
    fake_agents_pkg.librarian = fake_librarian
    monkeypatch.setitem(sys.modules, "scripts.agents", fake_agents_pkg)
    monkeypatch.setitem(sys.modules, "scripts.agents.librarian", fake_librarian)
    monkeypatch.setattr(backend_memex, "_ensure_memex_importable",
                        lambda: None)

    call_count = {"n": 0}
    def fake_write_entry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _DupErr(kwargs["librarian_output"]["key"], "existing-idx")
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
