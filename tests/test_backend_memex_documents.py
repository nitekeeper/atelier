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
from datetime import datetime as _datetime
from datetime import timezone as _tz

import pytest

from scripts import backend_memex


@pytest.fixture
def fake_memex_home(tmp_path, monkeypatch):
    """Point `MEMEX_HOME` at a clean tmp directory. Pulled only by tests
    that drive a code path which actually consults the env var (today
    none under test_backend_memex_documents.py, since the C1 importlib
    refactor removed the production `_memex_plugin_root` reliance on
    MEMEX_HOME and all callers now stub `_memex_module` directly)."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    # Memex v2.5.0's memex_home() validates that ~/.memex is under $HOME.
    # The explicit env var (with ALLOW_UNUSUAL) is the most reliable way
    # to point at a tmp path during tests.
    monkeypatch.setenv("MEMEX_HOME", str(memex_home))
    monkeypatch.setenv("MEMEX_HOME_ALLOW_UNUSUAL", "1")
    return memex_home


@pytest.fixture
def fake_memex_registry(fake_memex_home):
    """Write a registry.json with an `atelier` store entry into the
    tmp `fake_memex_home`. Pulled by tests that exercise Memex Core
    registry reads — none today (all `_memex_module` calls are stubbed
    per test). Kept as a focused fixture per QA N15 so future tests
    pull only the bytes they need."""
    (fake_memex_home / "registry.json").write_text(
        _json.dumps(
            {
                "atelier": {
                    "name": "atelier",
                    "path": (fake_memex_home / "atelier.db").as_posix(),
                    "schema_version": "v1",
                    "registered_at": _datetime.now(_tz.utc).isoformat(),
                },
            }
        )
    )
    return fake_memex_home


# Back-compat alias: most existing tests in this file used to take
# `fake_memex` for its env-var side effect. None of them actually need
# `registry.json`, so the alias points at the env-only fixture. Tests
# that genuinely need the registry should switch to `fake_memex_registry`.
@pytest.fixture
def fake_memex(fake_memex_home):
    return fake_memex_home


def test_load_memex_module_resolves_agents_librarian_without_collision(tmp_path, monkeypatch):
    """C1/I4: stand up a fake Memex plugin tree with the same shape as
    the real install (`scripts/agents/librarian.py` AS A PACKAGE) and
    verify production resolves it via `_memex_module("agents.librarian")`
    without the `scripts.agents` shadow from Atelier's own
    `scripts/agents.py` taking over.

    BEFORE the importlib refactor, the production code did
    `from scripts.agents import librarian` — which resolves to Atelier's
    flat `scripts/agents.py` module (no `librarian` attribute) and
    raises `ImportError`. This test exists specifically to prove that
    failure mode is gone.
    """
    plugin = tmp_path / "memex_plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "memex", "version": "test"})
    )
    scripts_dir = plugin / "scripts" / "agents"
    scripts_dir.mkdir(parents=True)
    (plugin / "scripts" / "__init__.py").write_text("")
    (scripts_dir / "__init__.py").write_text("")
    (scripts_dir / "librarian.py").write_text(
        "MARKER = 'fake-memex-librarian'\nclass DuplicateKeyError(Exception):\n    pass\n"
    )

    monkeypatch.setattr(backend_memex, "_memex_plugin_root", lambda: plugin)
    # Bust the lru_cache so this test's fake-plugin path doesn't pollute
    # adjacent tests (and isn't itself polluted by them).
    backend_memex._load_memex_module.cache_clear()
    monkeypatch.setattr(
        backend_memex._load_memex_module,
        "cache_clear",
        backend_memex._load_memex_module.cache_clear,
        raising=False,
    )

    mod = backend_memex._memex_module("agents.librarian")
    assert mod.MARKER == "fake-memex-librarian"
    # And the DuplicateKeyError class is reachable — production's
    # isinstance fallback in `_is_duplicate_key_error` will find it.
    assert isinstance(mod.DuplicateKeyError("k"), Exception)
    # Bust the cache afterward so the synthetic plugin doesn't bleed
    # into other tests that load the real Memex.
    backend_memex._load_memex_module.cache_clear()


def test_load_memex_module_scripts_db_shim_is_scoped_to_exec(tmp_path, monkeypatch):
    """T26 round-1 regression: when Memex's bundled scripts do
    `from scripts.db import get_connection` at module exec time, the
    loader must install `sys.modules['scripts.db']` for the duration
    of `exec_module` and remove it on exit.

    Atelier retired its own `scripts/db.py` in Plan 3 Task 9, so the
    parent symbol no longer exists; without the shim, every Memex
    CRUD module (roles, agents, stores, meetings, …) would crash with
    `ModuleNotFoundError: No module named 'scripts.db'` while being
    exec'd by `_load_memex_module`.

    This test stands up a minimal Memex plugin tree containing a
    self-contained `scripts/db.py` plus a `scripts/needs_db.py` that
    imports from it. We assert:
      1. The needs_db module loads cleanly (proves the shim is
         active during exec).
      2. `sys.modules['scripts.db']` is absent (or restored) after
         the loader returns (proves the shim does not pollute
         globals).
      3. The needs_db module's captured `get_connection` reference
         points at the Memex helper, not at any Atelier symbol.
    """
    plugin = tmp_path / "memex_plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        _json.dumps({"name": "memex", "version": "test"})
    )
    scripts_dir = plugin / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "__init__.py").write_text("")
    # Minimal `scripts/db.py` that mirrors Memex's stdlib-only shape.
    (scripts_dir / "db.py").write_text(
        "MEMEX_DB_MARKER = 'memex-db-shim'\n"
        "def get_connection(db_path):\n"
        "    return ('fake-conn', db_path)\n"
    )
    # A module whose top-level `from scripts.db import get_connection`
    # is the failure mode the shim must fix.
    (scripts_dir / "needs_db.py").write_text(
        "from scripts.db import get_connection\nMARKER = 'needs-db'\nBOUND = get_connection\n"
    )

    monkeypatch.setattr(backend_memex, "_memex_plugin_root", lambda: plugin)
    backend_memex._load_memex_module.cache_clear()

    # Snapshot pre-state so we can assert lack of pollution afterward.
    pre_present = "scripts.db" in sys.modules
    pre_value = sys.modules.get("scripts.db")

    try:
        needs_db = backend_memex._memex_module("needs_db")
    finally:
        # Always bust the cache so adjacent tests using the real Memex
        # plugin don't get the fake `db`/`needs_db` modules.
        backend_memex._load_memex_module.cache_clear()

    # (1) Shim was active during exec — module loaded successfully and
    # its captured reference is the Memex helper.
    assert needs_db.MARKER == "needs-db"
    assert needs_db.BOUND("test.db") == ("fake-conn", "test.db")
    # (2) Pollution check: `sys.modules['scripts.db']` looks exactly
    # like it did before the load (either both absent, or both equal
    # to the same prior object).
    post_present = "scripts.db" in sys.modules
    post_value = sys.modules.get("scripts.db")
    assert post_present is pre_present, (
        "shim leaked: 'scripts.db' presence changed across the "
        f"_memex_module call (pre={pre_present}, post={post_present})"
    )
    assert post_value is pre_value, (
        "shim leaked: 'scripts.db' identity changed across the _memex_module call"
    )


def test_write_document_validates_domain(fake_memex):
    """An unknown domain must be rejected before any Memex call."""
    with pytest.raises(ValueError, match="unknown domain"):
        backend_memex.write_document(
            domain="blog_post",  # not in DOMAINS
            title="x",
            body="x",
            metadata={},
            caller_agent_id="atelier-pm-1",
        )


def test_write_document_builds_librarian_output_and_writes(fake_memex, monkeypatch):
    captured = {}

    def fake_validate(d):
        captured["validated"] = d
        return d

    def fake_write_entry(
        *, payload, librarian_output, target_store, target_table, caller_agent_id, embedding
    ):
        captured["payload"] = payload
        captured["librarian_output"] = librarian_output
        captured["target_store"] = target_store
        captured["target_table"] = target_table
        captured["caller_agent_id"] = caller_agent_id
        return {
            "status": "ingested",
            "index_id": librarian_output["index_id"],
            "key": librarian_output["key"],
            "domain": librarian_output["domain"],
            "row_id": 42,
            "relations": [],
        }

    monkeypatch.setattr(backend_memex, "_memex_validate_output", fake_validate)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda text: b"\x00" * 16)
    # Stub key-construction helpers so tests don't need a live atelier.db.
    monkeypatch.setattr(
        backend_memex, "_resolve_project_slug", lambda pid: "myproj" if pid else None
    )
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))
    # Stub workspace_id derivation (issue #6 bug #3) — metadata here
    # does not pre-populate workspace_id, so write_document falls
    # back to the project lookup.
    monkeypatch.setattr(backend_memex, "_workspace_id_for_project", lambda pid: 5)

    result = backend_memex.write_document(
        domain="project_doc",
        title="Auth Design",
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


def test_write_document_folds_source_url_into_metadata(fake_memex, monkeypatch):
    """I2: `source_url` reaches `write_document` from the facade and
    MUST land in `metadata["source_url"]` so it persists to
    `~/.memex/index.db.documents.metadata` and contributes to the FTS5
    searchable blob via `_metadata_narrative`."""
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": k["librarian_output"]["index_id"],
                "key": k["librarian_output"]["key"],
                "domain": k["librarian_output"]["domain"],
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))
    # Stub workspace_id derivation (issue #6 bug #3).
    monkeypatch.setattr(backend_memex, "_workspace_id_for_project", lambda pid: 5)

    backend_memex.write_document(
        domain="adr",
        title="Use OAuth2",
        body="OAuth2 chosen over SAML.",
        metadata={"project_id": 1},
        caller_agent_id="atelier-pm-1",
        source_url="https://example.com/adr-007",
    )
    md = captured["librarian_output"]["metadata"]
    assert md["source_url"] == "https://example.com/adr-007"
    # And it joins the searchable narrative so FTS5 ranks documents
    # by URL hits.
    assert "https://example.com/adr-007" in captured["librarian_output"]["searchable"]


def test_write_task_targets_tasks_table_with_task_domain(fake_memex, monkeypatch):
    captured = {}
    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": k["librarian_output"]["index_id"],
                "key": k["librarian_output"]["key"],
                "domain": "task",
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(
        backend_memex, "_resolve_project_slug", lambda pid: "myproj" if pid else None
    )
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    backend_memex.write_task(
        title="Fix auth bug",
        description="OAuth returns 500",
        project_id=1,
        created_by="atelier-engineer-1",
        priority=5,
        notes="repro: hit /oauth/callback twice",
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
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": k["librarian_output"]["index_id"],
                "key": k["librarian_output"]["key"],
                "domain": "project",
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)

    backend_memex.write_project(
        workspace_id=1,
        slug="auth-svc",
        name="Auth Service",
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
    monkeypatch.setattr(
        backend_memex,
        "_memex_write_entry",
        lambda **k: (
            captured.update(k),
            {
                "status": "ingested",
                "index_id": k["librarian_output"]["index_id"],
                "key": k["librarian_output"]["key"],
                "domain": "meeting",
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))
    # Stub workspace_id derivation (issue #6 bug #4) — no project_id
    # so write_meeting falls back to the singleton-workspace lookup.
    monkeypatch.setattr(backend_memex, "_resolve_singleton_workspace_id", lambda: 1)

    backend_memex.write_meeting(
        title="Kickoff",
        date="2026-05-16",
        summary="Discussed scope.",
        decisions="Use OAuth2.",
        created_by="atelier-product-manager-1",
    )
    assert captured["target_table"] == "meeting_minutes"
    assert captured["librarian_output"]["domain"] == "meeting"
    assert "Discussed scope." in captured["librarian_output"]["searchable"]


def test_embedding_unavailable_is_swallowed_and_logged(fake_memex, monkeypatch):
    """When embeddings.encode raises EmbeddingUnavailable, persist with
    embedding=None AND record the skip via embeddings.log_skip (memex
    v2.4.1 contract). Untyped exceptions must propagate — see
    test_embedding_generic_exception_propagates below. Also asserts that
    the audit log call forwards `index_id` and `input_chars` (Nit N9)."""

    # The production code resolves `embeddings` via `_memex_module`.
    # Inject a fake into the module-load hook so `_is_embedding_unavailable`
    # picks up the class without consulting the real Memex install.
    class _FakeEmbeddingUnavailable(Exception):
        def __init__(self, reason: str, provider: str, detail: str = ""):
            self.reason = reason
            self.provider = provider
            self.detail = detail
            super().__init__(f"embedding unavailable: {reason}")

    fake_embeddings = types.ModuleType("embeddings")
    fake_embeddings.EmbeddingUnavailable = _FakeEmbeddingUnavailable
    fake_embeddings.encode = lambda text: b""
    fake_embeddings.log_skip = lambda *a, **k: None

    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "embeddings":
            return fake_embeddings
        return real_memex_module(name)

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    captured_embedding = {}
    captured_searchable = {}
    log_skip_calls = []

    def boom(text):
        captured_searchable["text"] = text
        raise _FakeEmbeddingUnavailable(
            reason="not_configured",
            provider="openai",
            detail="OPENAI_API_KEY unset",
        )

    def fake_log_skip(exc, *, caller_agent_id, index_id, input_chars):
        log_skip_calls.append(
            {
                "reason": exc.reason,
                "caller_agent_id": caller_agent_id,
                "index_id": index_id,
                "input_chars": input_chars,
            }
        )

    def fake_write_entry(**kwargs):
        captured_embedding["embedding"] = kwargs["embedding"]
        captured_embedding["index_id"] = kwargs["librarian_output"]["index_id"]
        return {
            "status": "ingested",
            "index_id": kwargs["librarian_output"]["index_id"],
            "key": kwargs["librarian_output"]["key"],
            "domain": kwargs["librarian_output"]["domain"],
            "row_id": 1,
            "relations": [],
        }

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_embed", boom)
    monkeypatch.setattr(backend_memex, "_memex_log_embedding_skip", fake_log_skip)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    backend_memex.write_task(
        title="x",
        description="y",
        project_id=1,
        created_by="atelier-product-manager-1",
    )
    assert captured_embedding["embedding"] is None
    assert len(log_skip_calls) == 1
    assert log_skip_calls[0]["reason"] == "not_configured"
    assert log_skip_calls[0]["caller_agent_id"] == "atelier-product-manager-1"
    # Nit N9: log_skip must forward index_id and input_chars (the size
    # of the searchable blob `_try_embed` was given).
    assert log_skip_calls[0]["index_id"] == captured_embedding["index_id"]
    assert log_skip_calls[0]["input_chars"] == len(captured_searchable["text"])


def test_embedding_generic_exception_propagates(fake_memex, monkeypatch):
    """Memex v2.4.1 narrowed the contract: only EmbeddingUnavailable is
    a degraded-mode signal. A generic exception means a real bug; it
    must not be silently treated as 'no embedding today.' Also asserts
    `_memex_log_embedding_skip` is NOT invoked on this path (Nit N10)."""

    class _FakeEmbeddingUnavailable(Exception):
        pass

    fake_embeddings = types.ModuleType("embeddings")
    fake_embeddings.EmbeddingUnavailable = _FakeEmbeddingUnavailable
    fake_embeddings.encode = lambda text: b""
    fake_embeddings.log_skip = lambda *a, **k: None

    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "embeddings":
            return fake_embeddings
        return real_memex_module(name)

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    def boom(text):
        raise RuntimeError("unexpected DB-side failure")

    from unittest.mock import MagicMock

    mock_log = MagicMock()

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_embed", boom)
    monkeypatch.setattr(backend_memex, "_memex_log_embedding_skip", mock_log)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: None)
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    with pytest.raises(RuntimeError, match="unexpected DB-side failure"):
        backend_memex.write_task(
            title="x",
            description="y",
            project_id=1,
            created_by="atelier-product-manager-1",
        )
    # Nit N10: a non-EmbeddingUnavailable exception must NOT invoke
    # the audit log (that's reserved for typed misses only).
    mock_log.assert_not_called()


def test_duplicate_key_triggers_single_retry(fake_memex, monkeypatch):
    """Memex v2.3.0+ enforces UNIQUE on documents.key. On a race between
    seq-allocation and write, `librarian.write_entry` raises
    DuplicateKeyError. `_atelier_write` retries ONCE with a fresh seq."""
    # Stand up a fake librarian module so DuplicateKeyError is reachable
    # via _is_duplicate_key_error's isinstance fallback. The class is
    # named `DuplicateKeyError` so the structural type-name probe also
    # matches (which is what production's first-line check uses).
    fake_librarian = types.ModuleType("agents.librarian")

    class DuplicateKeyError(Exception):
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

    fake_librarian.DuplicateKeyError = DuplicateKeyError
    # Inject into backend_memex's loader cache via the `_memex_module`
    # hook so production's `_is_duplicate_key_error` fallback can locate
    # the class via `_memex_module("agents.librarian")` without touching
    # the real Memex install.
    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "agents.librarian":
            return fake_librarian
        return real_memex_module(name)

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    call_count = {"n": 0}

    def fake_write_entry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise DuplicateKeyError(kwargs["librarian_output"]["key"], "existing-idx")
        return {
            "status": "ingested",
            "index_id": kwargs["librarian_output"]["index_id"],
            "key": kwargs["librarian_output"]["key"],
            "domain": kwargs["librarian_output"]["domain"],
            "row_id": 7,
            "relations": [],
        }

    seq_calls = {"n": 0}

    def fake_next_seq(*a, **k):
        seq_calls["n"] += 1
        return seq_calls["n"]  # 1, then 2 on retry

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "myproj")
    monkeypatch.setattr(backend_memex, "_next_seq", fake_next_seq)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    result = backend_memex.write_task(
        title="Fix bug",
        description="x",
        project_id=1,
        created_by="atelier-product-manager-1",
    )
    assert call_count["n"] == 2  # retried exactly once
    assert result["row_id"] == 7
    assert result["key"].endswith("-fix-bug-2")  # second seq used


def test_duplicate_key_retry_only_once(fake_memex, monkeypatch):
    """N11: prove the retry is bounded to ONE re-attempt. If the
    re-attempt also collides on UNIQUE(documents.key), the second
    DuplicateKeyError must propagate — we don't loop forever."""
    fake_librarian = types.ModuleType("agents.librarian")

    class DuplicateKeyError(Exception):
        def __init__(self, key, existing_index_id=None):
            self.key = key
            self.existing_index_id = existing_index_id
            super().__init__(f"duplicate {key}")

    fake_librarian.DuplicateKeyError = DuplicateKeyError
    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "agents.librarian":
            return fake_librarian
        return real_memex_module(name)

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    call_count = {"n": 0}

    def fake_write_entry(**kwargs):
        call_count["n"] += 1
        # Both attempts collide.
        raise DuplicateKeyError(kwargs["librarian_output"]["key"], "existing")

    monkeypatch.setattr(backend_memex, "_memex_validate_output", lambda d: d)
    monkeypatch.setattr(backend_memex, "_memex_write_entry", fake_write_entry)
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "myproj")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    with pytest.raises(DuplicateKeyError):
        backend_memex.write_task(
            title="Fix bug",
            description="x",
            project_id=1,
            created_by="atelier-pm-1",
        )
    # Exactly two attempts — the initial write + ONE retry. If we ever
    # see three here, the retry loop has lost its bound.
    assert call_count["n"] == 2


def test_canonical_key_format(fake_memex, monkeypatch):
    """Verify spec §6.7 key shape: workspace/project/domain/date-title-seq."""
    captured = {}
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
                "domain": "task",
                "row_id": 1,
                "relations": [],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "auth-svc")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 3)
    monkeypatch.setattr(backend_memex, "_auto_relations", lambda md, r: list(r or []))

    backend_memex.write_task(
        title="Fix OAuth Race",
        description="x",
        project_id=1,
        created_by="atelier-engineer-1",
    )
    key = captured["librarian_output"]["key"]
    # Format: atelier/auth-svc/task/YYYY-MM-DD-fix-oauth-race-3
    parts = key.split("/")
    assert parts[0] == "atelier"
    assert parts[1] == "auth-svc"
    assert parts[2] == "task"
    assert parts[3].endswith("-fix-oauth-race-3")


def test_auto_part_of_relation_when_project_id_in_metadata(fake_memex, monkeypatch):
    """Per spec §6.9, atelier writes should auto-populate `part_of`
    edges from the new document to the owning project's document."""
    captured = {}
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
                "domain": "task",
                "row_id": 1,
                "relations": k["librarian_output"]["relations"],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "p")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    # Simulate one project document found for project_id=1.
    monkeypatch.setattr(
        backend_memex,
        "_auto_relations",
        lambda md, r: (
            [*list(r or []), {"rel_type": "part_of", "to_index_id": "proj-idx-1"}]
            if (md or {}).get("project_id")
            else list(r or [])
        ),
    )

    backend_memex.write_task(
        title="t",
        description="d",
        project_id=1,
        created_by="atelier-engineer-1",
    )
    rels = captured["librarian_output"]["relations"]
    assert any(r["rel_type"] == "part_of" and r["to_index_id"] == "proj-idx-1" for r in rels)


def test_auto_relations_runs_real_logic_and_emits_part_of_edge(fake_memex, monkeypatch):
    """I6: exercise the real `_auto_relations` production logic (NOT a
    stub). Seed `index.documents` with a `project` row whose
    metadata.project_id matches the new write, then call write_task and
    confirm the resulting librarian_output carries the auto-discovered
    `part_of` edge."""
    # Fake `scripts.stores` so _auto_relations' SELECT returns our seed.
    seeded_rows = [{"index_id": "proj-doc-uuid-1"}]
    queries_seen: list = []

    def fake_query(store, sql, params):
        queries_seen.append((store, sql, params))
        if store == "index" and "domain" in sql:
            # _auto_relations is the only caller that touches index.documents
            # with a JSON metadata predicate in this test.
            return list(seeded_rows)
        return []

    fake_stores = types.SimpleNamespace(query=fake_query)
    real_memex_module = backend_memex._memex_module

    def fake_memex_module(name: str):
        if name == "stores":
            return fake_stores
        return real_memex_module(name)

    monkeypatch.setattr(backend_memex, "_memex_module", fake_memex_module)

    captured = {}
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
                "domain": "task",
                "row_id": 1,
                "relations": k["librarian_output"]["relations"],
            },
        )[1],
    )
    monkeypatch.setattr(backend_memex, "_memex_embed", lambda t: None)
    monkeypatch.setattr(backend_memex, "_resolve_project_slug", lambda pid: "p")
    monkeypatch.setattr(backend_memex, "_next_seq", lambda *a, **k: 1)
    # Intentionally DO NOT stub _auto_relations — that's the function
    # under test on this path.

    backend_memex.write_task(
        title="t",
        description="d",
        project_id=42,
        created_by="atelier-engineer-1",
    )
    rels = captured["librarian_output"]["relations"]
    # The auto-edge points at the seeded project document.
    assert any(
        r["rel_type"] == "part_of" and r["to_index_id"] == "proj-doc-uuid-1" for r in rels
    ), rels
    # And the SQL we ran really did query index.documents for the
    # project domain with project_id=42 — i.e. we exercised the real
    # `_auto_relations`, not a leftover stub.
    matching = [
        q
        for q in queries_seen
        if q[0] == "index" and "domain" in q[1] and "project" in q[2] and 42 in q[2]
    ]
    assert matching, f"no SELECT against index.documents seen: {queries_seen}"


# ──────────────────────────────────────────────────────────────────────────
# atelier#30 — _auto_relations workspace_id filter
# ──────────────────────────────────────────────────────────────────────────


def test_auto_relations_adds_workspace_filter_when_metadata_has_workspace_id(
    fake_memex, monkeypatch
):
    """atelier#30: when metadata carries workspace_id, the project-doc
    query MUST add `AND json_extract(metadata, '$.workspace_id') = ?`
    so cross-workspace project_id collisions cannot create part_of
    edges to the wrong workspace's project document."""
    queries_seen: list = []

    def fake_query(store, sql, params):
        queries_seen.append((store, sql, params))
        return []  # no matching project, edge not auto-added

    fake_stores = types.SimpleNamespace(query=fake_query)
    monkeypatch.setattr(
        backend_memex,
        "_memex_module",
        lambda name: fake_stores if name == "stores" else backend_memex._memex_module(name),
    )

    backend_memex._auto_relations(metadata={"project_id": 42, "workspace_id": 7}, explicit=[])
    # Exactly one query — the project-doc lookup.
    assert len(queries_seen) == 1
    store, sql, params = queries_seen[0]
    assert store == "index"
    assert "$.project_id" in sql
    assert "$.workspace_id" in sql, f"workspace filter missing from SQL: {sql}"
    assert params == ("project", 42, 7)


def test_auto_relations_omits_workspace_filter_when_metadata_lacks_workspace_id(
    fake_memex, monkeypatch
):
    """Backward compat: pre-#30 callers that didn't add workspace_id to
    metadata must still get the single-filter (project_id only) query so
    nothing breaks in the single-workspace deployment."""
    queries_seen: list = []

    def fake_query(store, sql, params):
        queries_seen.append((store, sql, params))
        return []

    fake_stores = types.SimpleNamespace(query=fake_query)
    monkeypatch.setattr(
        backend_memex,
        "_memex_module",
        lambda name: fake_stores if name == "stores" else backend_memex._memex_module(name),
    )

    backend_memex._auto_relations(metadata={"project_id": 42}, explicit=[])
    assert len(queries_seen) == 1
    _store, sql, params = queries_seen[0]
    assert "$.workspace_id" not in sql, f"unexpected workspace filter in SQL: {sql}"
    assert params == ("project", 42)
