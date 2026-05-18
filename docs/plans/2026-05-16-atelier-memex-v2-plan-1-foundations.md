# Atelier ↔ Memex v2 Retrofit — Plan 1 of 4: Foundations (Wave 0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay down the scaffolding Wave 1 / Wave 1' / Wave 2 will build on — persistence-facade skeleton, mode detector, seed data, and a clean split between migrations that ship to Memex Core vs. migrations local-mode only consumes.

**Architecture:** This wave produces only the contracts and scaffolding. No business logic is rewired yet. Every task in this plan is independent and can be dispatched in **one parallel batch** of 7 subagents.

**Tech Stack:** Python 3.10+, pytest, sqlite3 (stdlib), pathlib, JSON for seed data.

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md](../specs/2026-05-16-atelier-memex-v2-retrofit-design.md) §§4-5, §11.

---

## Parallel dispatch map

```
Wave 0 — all parallel, 7 independent tasks
┌────────────────┐ ┌────────────────┐ ┌─────────────────┐ ┌────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│ Task 1         │ │ Task 2         │ │ Task 3          │ │ Task 4         │ │ Task 5          │ │ Task 6          │ │ Task 7          │
│ backend.py     │ │ mode_detector  │ │ roles seed JSON │ │ agents seed    │ │ migrations split│ │ domain          │ │ workspace_root  │
│ facade skeleton│ │ + tests        │ │ + loader        │ │ JSON + loader  │ │ shared/local-only│ │ vocabulary doc  │ │ + find_git_root │
└────────────────┘ └────────────────┘ └─────────────────┘ └────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘
```

All 7 tasks touch disjoint files. Dispatch all seven as parallel subagents per `superpowers:dispatching-parallel-agents`.

---

### Task 1: Persistence facade skeleton

**Files:**
- Create: `scripts/backend.py`
- Test: `tests/test_backend_skeleton.py`

The facade defines the API surface that Wave 1 (Memex) and Wave 1' (Local) will both implement. In Wave 0 it raises `NotImplementedError` for every method but its signature is the contract per spec §4.3. Mode-routing comes from Task 2.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backend_skeleton.py
"""Wave 0 contract tests for the persistence facade.

Each method must exist with the expected signature and raise
NotImplementedError when called. Wave 1 / Wave 1' replace the bodies.
The full surface mirrors spec §4.3 lines 187-223.
"""
import pytest
import inspect
from scripts import backend


EXPECTED_METHODS = [
    # Document-shaped writes — Tier 2
    "write_project",
    "write_document",
    "write_task",
    "write_meeting",
    # Operational state — Tier 1
    "upsert_session",
    "transition_phase",
    "update_task_status",
    "record_phase_bypass",
    # Workspace + project resolution
    "find_or_create_workspace",
    "find_workspace_by_identity",
    "list_workspaces",
    "find_project",
    "list_projects",
    # Reads
    "find_documents",
    "get_task",
    "list_tasks",
    "get_document",
    "lookup_index_id_by_source_ref",
    # Idempotent role/agent helpers — used by scripts/seed_roles.py
    # (Plan 3) and the bootstrap path. Both must be safe to call on a
    # populated DB.
    "find_or_create_role",
    "find_or_create_agent",
]


def test_facade_module_exists():
    for name in EXPECTED_METHODS:
        assert hasattr(backend, name), f"backend.{name} missing"


@pytest.mark.parametrize("fn_name,kwargs", [
    ("write_project", dict(workspace_id=1, slug="proj", name="Proj",
                           description="d", created_by="a")),
    ("write_document", dict(workspace_id=1, project_id=1, domain="design",
                            subdomain=None, title="t", body="b",
                            metadata={}, caller_agent_id="a")),
    ("write_task", dict(workspace_id=1, project_id=1, title="t",
                        description="d", subdomain=None, created_by="a")),
    ("write_meeting", dict(workspace_id=1, project_id=1, title="t",
                           date="2026-05-16", summary="s", decisions="d",
                           subdomain=None, created_by="a")),
    ("upsert_session", dict(project_id=1, agent_id="a", phase="design:open")),
    ("transition_phase", dict(project_id=1, to_phase="plan:open", agent_id="a")),
    ("update_task_status", dict(task_id=1, status="in-progress")),
    ("record_phase_bypass", dict(project_id=1, from_phase="x", to_phase="y",
                                 reason="r", agent_id="a")),
    ("find_or_create_workspace", dict(identity="repo:x", slug="x", name="X")),
    ("find_workspace_by_identity", dict(identity="repo:x")),
    ("list_workspaces", dict()),
    ("find_project", dict(workspace_id=1, slug="proj")),
    ("list_projects", dict(workspace_id=1)),
    ("find_documents", dict(query="q")),
    ("get_task", dict(task_id=1)),
    ("list_tasks", dict(project_id=1)),
    ("get_document", dict(doc_id=1)),
    ("lookup_index_id_by_source_ref",
     dict(source_ref="atelier:tasks:1")),
    ("find_or_create_role", dict(name="Product Manager",
                                  description="PM")),
    ("find_or_create_agent", dict(agent_id="atelier-pm-1", name="PM",
                                   role_id=1, profile="pm")),
])
def test_method_raises_not_implemented(fn_name, kwargs):
    fn = getattr(backend, fn_name)
    with pytest.raises(NotImplementedError):
        fn(**kwargs)


def test_methods_accept_keyword_args_only():
    """Wave 1 / 1' will swap implementations; keyword-only signatures
    prevent positional-arg drift between backends."""
    for name in EXPECTED_METHODS:
        sig = inspect.signature(getattr(backend, name))
        for p in sig.parameters.values():
            # All params should be KEYWORD_ONLY or have a default
            assert p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD), \
                f"{name}.{p.name} should be keyword-callable"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_backend_skeleton.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.backend'`.

- [ ] **Step 3: Create the facade module**

```python
# scripts/backend.py
"""Persistence facade.

Wave 0 ships only the signatures. Wave 1 (Memex backend) and Wave 1'
(Local backend) replace the bodies with mode-dispatched implementations.

Every method is keyword-only to prevent positional-arg drift between
the two backends as they evolve. Surface mirrors spec §4.3.
"""
from __future__ import annotations
from typing import Any


def _not_implemented(name: str) -> None:
    raise NotImplementedError(
        f"backend.{name} has no implementation yet. "
        f"Wave 1 (Memex) or Wave 1' (Local) supplies the body."
    )


# ── Document-shaped writes — Tier 2 ─────────────────────────────────────

def write_project(*, workspace_id: int, slug: str, name: str,
                  description: str, created_by: str) -> dict:
    _not_implemented("write_project")


def write_document(*, workspace_id: int, project_id: int,
                   domain: str, subdomain: str | None,
                   title: str, body: str,
                   metadata: dict, caller_agent_id: str,
                   source_url: str | None = None,
                   relations: list[dict] = ()) -> dict:
    _not_implemented("write_document")


def write_task(*, workspace_id: int, project_id: int,
               title: str, description: str,
               subdomain: str | None, created_by: str,
               assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None,
               relations: list[dict] = ()) -> dict:
    _not_implemented("write_task")


def write_meeting(*, workspace_id: int, project_id: int | None,
                  title: str, date: str, summary: str,
                  decisions: str, subdomain: str | None,
                  created_by: str,
                  relations: list[dict] = ()) -> dict:
    _not_implemented("write_meeting")


# ── Operational state — Tier 1 ──────────────────────────────────────────

def upsert_session(*, project_id: int, agent_id: str, phase: str | None = None,
                   current_tasks: str | None = None,
                   accomplished: str | None = None,
                   next_action: str | None = None,
                   status: str = "in-progress",
                   pm_notes: str | None = None) -> dict:
    _not_implemented("upsert_session")


def transition_phase(*, project_id: int, to_phase: str,
                     agent_id: str, bypass_reason: str | None = None) -> dict:
    _not_implemented("transition_phase")


def update_task_status(*, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    _not_implemented("update_task_status")


def record_phase_bypass(*, project_id: int, from_phase: str, to_phase: str,
                        reason: str, agent_id: str) -> dict:
    _not_implemented("record_phase_bypass")


# ── Workspace + project resolution ──────────────────────────────────────

def find_or_create_workspace(*, identity: str, slug: str, name: str,
                             description: str | None = None) -> dict:
    _not_implemented("find_or_create_workspace")


def find_workspace_by_identity(*, identity: str) -> dict | None:
    _not_implemented("find_workspace_by_identity")


def list_workspaces() -> list[dict]:
    _not_implemented("list_workspaces")


def find_project(*, workspace_id: int, slug: str) -> dict | None:
    _not_implemented("find_project")


def list_projects(*, workspace_id: int) -> list[dict]:
    _not_implemented("list_projects")


# ── Reads ───────────────────────────────────────────────────────────────

def find_documents(*, query: str, workspace_id: int | None = None,
                   project_id: int | None = None,
                   domain: str | None = None, subdomain: str | None = None,
                   limit: int = 10) -> list[dict]:
    _not_implemented("find_documents")


def get_task(*, task_id: int) -> dict | None:
    _not_implemented("get_task")


def list_tasks(*, project_id: int, status: str | None = None) -> list[dict]:
    _not_implemented("list_tasks")


def get_document(*, doc_id: int) -> dict | None:
    _not_implemented("get_document")


def lookup_index_id_by_source_ref(*, source_ref: str) -> str | None:
    """Reverse-lookup for the idempotent-migration use case.

    Plan 4's `scripts/migrate_to_memex.py` writes each migrated row with
    `metadata["source_ref"] = "atelier:<table>:<local_id>"`. On a rerun
    after a partial outage, the migrator calls this method first; if it
    returns a non-None index_id, the row already landed and is skipped
    (avoiding `librarian.DuplicateKeyError`).

    Returns the Memex Index `index_id` (str) on hit; None on miss.
    """
    _not_implemented("lookup_index_id_by_source_ref")


# ── Idempotent role / agent helpers ──────────────────────────────────────
#
# Used by `scripts/seed_roles.py` (Plan 3) and the Memex-mode bootstrap.
# Both must be safe to call on a populated DB — return the existing row
# instead of raising IntegrityError.

def find_or_create_role(*, name: str, description: str) -> dict:
    """Return the role row with this `name`, creating it if absent.
    Idempotent."""
    _not_implemented("find_or_create_role")


def find_or_create_agent(*, agent_id: str, name: str, role_id: int,
                         profile: str) -> dict:
    """Return the agent row with this `agent_id`, creating it if absent.
    Idempotent."""
    _not_implemented("find_or_create_agent")
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_backend_skeleton.py -v
```
Expected: 22 passed (1 facade-module test + 20 parametrized NotImplementedError tests + 1 keyword-only signature test). The parametrized count includes `lookup_index_id_by_source_ref`, `find_or_create_role`, and `find_or_create_agent` per the cross-plan dependency audit.

- [ ] **Step 5: Commit**

```bash
git add scripts/backend.py tests/test_backend_skeleton.py
git commit -m "feat(backend): wave-0 facade skeleton — signatures only"
```

---

### Task 2: Mode-detection module

**Files:**
- Create: `scripts/mode_detector.py`
- Test: `tests/test_mode_detector.py`

Detects whether Memex v2 is installed and reachable. Cached per-process. Per spec §4.2.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mode_detector.py
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from scripts import mode_detector


@pytest.fixture(autouse=True)
def clear_cache():
    mode_detector._clear_cache()
    yield
    mode_detector._clear_cache()


def test_returns_local_when_no_memex_home(tmp_path):
    with patch("scripts.mode_detector._memex_home", return_value=tmp_path / "absent"):
        assert mode_detector.detect_mode() == "local"


def test_returns_local_when_registry_absent(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector.detect_mode() == "local"


def test_returns_local_when_registry_present_but_plugin_unreachable(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=False):
        assert mode_detector.detect_mode() == "local"


def test_returns_memex_when_both_present(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=True):
        assert mode_detector.detect_mode() == "memex"


def test_result_is_cached(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=True) as m:
        mode_detector.detect_mode()
        mode_detector.detect_mode()
        mode_detector.detect_mode()
        assert m.call_count == 1, "second call must hit cache"


def test_clear_cache_recomputes(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "registry.json").write_text("{}")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home), \
         patch("scripts.mode_detector._memex_plugin_reachable", return_value=True):
        mode_detector.detect_mode()
    mode_detector._clear_cache()
    with patch("scripts.mode_detector._memex_home", return_value=tmp_path / "absent"):
        assert mode_detector.detect_mode() == "local"


def _make_fake_plugin_root(root: Path) -> None:
    """Build the minimum filesystem structure _memex_plugin_reachable() validates."""
    root.mkdir(parents=True, exist_ok=True)
    cp = root / ".claude-plugin"
    cp.mkdir()
    (cp / "plugin.json").write_text(json.dumps({"name": "memex", "version": "2.5.1"}))


def test_plugin_reachable_true_with_valid_config_pin(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.5.1"
    _make_fake_plugin_root(plugin_root)
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is True


def test_plugin_reachable_false_when_config_missing(tmp_path):
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_config_invalid_json(tmp_path):
    """Defensive — half-written config.json (e.g., Memex crashed mid-write)."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text("{not-valid-json")
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_pinned_root_missing(tmp_path):
    """Stale pin — Memex was uninstalled but config.json wasn't cleaned up."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    (memex_home / "config.json").write_text(json.dumps(
        {"plugin_root": str(tmp_path / "deleted-cache-entry")}
    ))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_manifest_wrong_name(tmp_path):
    """Defensive — the pin points at a real plugin, just not the memex one."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "some-other-plugin"
    plugin_root.mkdir()
    (plugin_root / ".claude-plugin").mkdir()
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "not-memex"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_false_when_memex_below_api_floor(tmp_path):
    """Defensive — pin points at memex but version < 2.2.0 (no
    caller-built librarian_output). Returns False so atelier degrades
    to local mode rather than crashing on the first Tier 2 write."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.1.0"
    _make_fake_plugin_root(plugin_root)
    # Overwrite the manifest with a sub-floor version
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.1.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False


def test_plugin_reachable_true_when_memex_at_api_floor(tmp_path):
    """Exact-floor version (2.2.0) passes."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-2.2.0"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex", "version": "2.2.0"})
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is True


def test_plugin_reachable_false_when_manifest_version_unparseable(tmp_path):
    """Defensive — manifest has no version field or malformed value."""
    memex_home = tmp_path / ".memex"
    memex_home.mkdir()
    plugin_root = tmp_path / "memex-malformed"
    _make_fake_plugin_root(plugin_root)
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "memex"})  # no version key
    )
    (memex_home / "config.json").write_text(json.dumps({"plugin_root": str(plugin_root)}))
    with patch("scripts.mode_detector._memex_home", return_value=memex_home):
        assert mode_detector._memex_plugin_reachable() is False
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_mode_detector.py -v
```
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the detector**

```python
# scripts/mode_detector.py
"""Detect whether Memex v2 is installed + reachable.

Result is cached for the lifetime of the Python process. Each Atelier
command invocation re-imports and therefore re-detects.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Literal

Mode = Literal["memex", "local"]

_cached: Mode | None = None


def _memex_home() -> Path:
    return Path.home() / ".memex"


_MEMEX_API_FLOOR = (2, 2, 0)  # spec §6 prerequisite — caller-built librarian_output


def _parse_version_tuple(s: str) -> tuple[int, int, int] | None:
    """Parse a "X.Y.Z" string into a (major, minor, patch) tuple of ints.
    Returns None if the string isn't parseable (e.g., empty, malformed)."""
    if not isinstance(s, str):
        return None
    parts = s.strip().split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _memex_plugin_reachable() -> bool:
    """True if Memex itself has resolved + pinned its plugin root in
    ~/.memex/config.json (Memex v2.5.0+ contract — see spec §4.2 rationale)
    AND the pinned memex version meets the v2.2.0 API floor (spec §6
    prerequisite — caller-built `librarian_output`).

    The pin is written by Memex's Step 0.2 preflight after it has already
    validated that the plugin directory exists, scripts/install.py is a
    regular file, and .claude-plugin/plugin.json declares name="memex".
    Atelier re-verifies the manifest because the cached pin could go
    stale between Memex invocations (e.g., a version directory was
    deleted during cache cleanup), AND re-checks the version because a
    user could (in theory) downgrade memex below atelier's API floor.
    Returning False on under-floor versions degrades to local mode
    rather than crashing on the first Tier 2 write.
    """
    config = _memex_home() / "config.json"
    if not config.exists():
        return False
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    plugin_root = data.get("plugin_root")
    if not isinstance(plugin_root, str):
        return False
    root = Path(plugin_root)
    if not root.is_dir():
        return False
    manifest = root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return False
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if manifest_data.get("name") != "memex":
        return False
    version = _parse_version_tuple(manifest_data.get("version", ""))
    if version is None or version < _MEMEX_API_FLOOR:
        return False
    return True


def detect_mode() -> Mode:
    global _cached
    if _cached is not None:
        return _cached
    home = _memex_home()
    if not home.exists() or not (home / "registry.json").exists():
        _cached = "local"
    elif not _memex_plugin_reachable():
        _cached = "local"
    else:
        _cached = "memex"
    return _cached


def _clear_cache() -> None:
    """Test-only — force re-detection."""
    global _cached
    _cached = None
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_mode_detector.py -v
```
Expected: 14 passed (11 from the prior amendments + 3 new version-floor tests: under-floor, exact-floor, unparseable-version).

- [ ] **Step 5: Commit**

```bash
git add scripts/mode_detector.py tests/test_mode_detector.py
git commit -m "feat(mode-detector): wave-0 Memex-presence detection with caching"
```

---

### Task 3: Atelier role seed data + loader

**Files:**
- Create: `templates/roles.json`
- Create: `scripts/seed_data.py` (only the role half; agents added in Task 4 by a different subagent — they edit non-conflicting regions)
- Test: `tests/test_seed_roles.py` (extend with a new test; the existing file already covers v1 role seeding)

This packages Atelier's full role catalog as data so both bootstrap paths (Memex `register-role` and Local `roles` INSERT) consume the same source of truth.

**Source of truth:** the existing `scripts/seed_roles.py:ROLES` list (61 entries as of v1.0.13). Plan 1 mechanically extracts `{role_name, role_desc}` from every entry and writes a JSON array. The canonical PM name is **"Product Manager"** — this matches `tests/test_seed_roles.py:45-51`'s `test_seed_pm_role_exists` assertion and `scripts/seed_roles.py:22`. Do **not** rename to "Project Manager".

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_roles.py — APPEND to existing file
import json
from pathlib import Path
from scripts.seed_data import load_role_seed

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def test_role_seed_file_exists():
    assert (TEMPLATES_DIR / "roles.json").exists()


def test_role_seed_returns_list_of_dicts():
    roles = load_role_seed()
    assert isinstance(roles, list)
    # The shipped catalog has ~61 personas (see scripts/seed_roles.py ROLES).
    # We assert a floor of 46 to keep the test resilient to additions/removals.
    assert len(roles) >= 46, f"expected at least 46 roles, got {len(roles)}"
    for r in roles:
        assert {"name", "description"} <= r.keys()


def test_role_seed_has_canonical_atelier_roles():
    roles = load_role_seed()
    names = {r["name"] for r in roles}
    # Canonical PM name is "Product Manager" — see scripts/seed_roles.py:22
    # and the existing test_seed_pm_role_exists in this file.
    assert "Product Manager" in names
    assert "Software Architect" in names


def test_role_seed_names_are_unique():
    roles = load_role_seed()
    names = [r["name"] for r in roles]
    assert len(names) == len(set(names))


def test_role_seed_matches_seed_roles_module():
    """The JSON file is the source of truth; this test pins parity with the
    existing seed_roles.ROLES list so Plan 4's migrator can swap one for
    the other without behavior change."""
    from scripts.seed_roles import ROLES as LEGACY_ROLES
    json_names = {r["name"] for r in load_role_seed()}
    legacy_names = {r["role_name"] for r in LEGACY_ROLES}
    assert json_names == legacy_names
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_seed_roles.py -v -k "role_seed"
```
Expected: FAIL — file missing, loader missing.

- [ ] **Step 3: Generate `templates/roles.json` from `scripts/seed_roles.py`**

Generation procedure (run once during plan execution; not committed as a script):

```python
# one-shot generator — do not commit
import json
from pathlib import Path
from scripts.seed_roles import ROLES

# Deduplicate by role_name (preserve first occurrence). The legacy list has
# one role per persona; multiple personas may share a role name (e.g.,
# multiple engineers). Roles table is keyed by name (UNIQUE).
seen = {}
for entry in ROLES:
    if entry["role_name"] not in seen:
        seen[entry["role_name"]] = entry["role_desc"]

out = {"roles": [{"name": n, "description": d} for n, d in seen.items()]}
Path("templates/roles.json").write_text(json.dumps(out, indent=2) + "\n",
                                        encoding="utf-8")
```

**Result:** `templates/roles.json` shape:

```json
{
  "roles": [
    {
      "name": "Product Manager",
      "description": "Coordination hub. Bridges user ↔ agents. Manages sessions, tasks, and priorities."
    },
    {
      "name": "Software Architect",
      "description": "Owns system-level architecture decisions, API contracts, and cross-cutting standards."
    },
    {
      "name": "Systems Architect",
      "description": "Owns platform-level and infrastructure-level architecture — the layer beneath application services."
    },
    ...
  ]
}
```

The description strings are **verbatim** from `scripts/seed_roles.py:ROLES[i].role_desc` — do not rewrite. This guarantees the JSON file and the legacy seed agree on role copy, which the `test_role_seed_matches_seed_roles_module` test pins.

- [ ] **Step 4: Create the loader (role portion only)**

```python
# scripts/seed_data.py
"""Load Atelier's shipped role + agent seed data.

Both Memex bootstrap (memex:core:register-role / register-agent) and
Local-mode INSERT paths read from the same JSON files in templates/.
"""
from __future__ import annotations
import json
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def load_role_seed() -> list[dict]:
    """Return the canonical role catalog as a list of {name, description}."""
    data = json.loads((_TEMPLATES / "roles.json").read_text(encoding="utf-8"))
    return data["roles"]


# Agent loader added by Task 4.
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/test_seed_roles.py -v -k "role_seed"
```
Expected: 5 passed (file_exists, returns_list_of_dicts, has_canonical_atelier_roles, names_are_unique, matches_seed_roles_module).

- [ ] **Step 6: Commit**

```bash
git add templates/roles.json scripts/seed_data.py tests/test_seed_roles.py
git commit -m "feat(seed): wave-0 Atelier role seed JSON + loader"
```

---

### Task 4: Atelier shipped agents seed + loader

**Files:**
- Create: `templates/agents/` directory with one JSON file per persona (~61 files, one per `scripts/seed_roles.py:ROLES` entry)
- Modify: `scripts/seed_data.py` (append agent loader; non-conflicting region below Task 3's sentinel)
- Create: `tests/test_seed_agents.py`

This task is independent of Task 3 in terms of files **except** for `scripts/seed_data.py`. The append region is below a sentinel comment Task 3 leaves in place (`# Agent loader added by Task 4.`). The two subagents must not edit each other's regions. If running them in parallel, Task 4 appends; Task 3 writes the file with the sentinel.

**Source of truth:** the existing `scripts/seed_roles.py:ROLES` list. Each entry already carries `agent_id`, `agent_name`, `agent_profile`, and `role_name` — exactly the four fields the JSON shape needs. Plan 1 mechanically writes one file per entry.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_agents.py
from pathlib import Path
import json
from scripts.seed_data import load_agent_seed

TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "agents"


def test_agents_directory_exists():
    assert TEMPLATES_DIR.is_dir()


def test_agents_directory_has_expected_count():
    """One JSON file per persona in scripts/seed_roles.py:ROLES."""
    files = list(TEMPLATES_DIR.glob("*.json"))
    assert len(files) >= 46, f"expected at least 46 agent files, got {len(files)}"


def test_load_agent_seed_returns_at_least_46_agents():
    agents = load_agent_seed()
    assert len(agents) >= 46


def test_each_agent_has_required_fields():
    for a in load_agent_seed():
        assert {"agent_id", "name", "role_name", "profile"} <= a.keys()
        assert isinstance(a["profile"], str) and len(a["profile"]) > 100


def test_agent_ids_unique():
    agents = load_agent_seed()
    ids = [a["agent_id"] for a in agents]
    assert len(ids) == len(set(ids))


def test_agent_role_names_match_role_seed():
    """Every agent.role_name must resolve to a known role in roles.json.
    The loader does not perform the resolution (that's done at bootstrap
    after memex:core:register-role returns the new role_id int PK); this
    test only enforces referential integrity at the name level."""
    from scripts.seed_data import load_role_seed
    role_names = {r["name"] for r in load_role_seed()}
    for a in load_agent_seed():
        assert a["role_name"] in role_names, \
            f"agent {a['agent_id']} references unknown role {a['role_name']}"


def test_agent_seed_matches_seed_roles_module():
    """Parity with the legacy seed_roles.ROLES list — Plan 4's migrator
    needs to know the JSON files are an exact mirror."""
    from scripts.seed_roles import ROLES as LEGACY_ROLES
    json_ids = {a["agent_id"] for a in load_agent_seed()}
    legacy_ids = {r["agent_id"] for r in LEGACY_ROLES}
    assert json_ids == legacy_ids
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_seed_agents.py -v
```
Expected: FAIL — directory + loader missing.

- [ ] **Step 3: Generate one JSON file per persona**

Generation procedure (run once during plan execution; not committed):

```python
# one-shot generator — do not commit
import json
from pathlib import Path
from scripts.seed_roles import ROLES

out_dir = Path("templates/agents")
out_dir.mkdir(parents=True, exist_ok=True)

for entry in ROLES:
    payload = {
        "agent_id":  entry["agent_id"],
        "name":      entry["agent_name"],
        "role_name": entry["role_name"],
        "profile":   entry["agent_profile"],
    }
    (out_dir / f"{entry['agent_id']}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
```

**File shape** (`templates/agents/pm-1.json`):

```json
{
  "agent_id": "pm-1",
  "name": "Dr. Priya Nair",
  "role_name": "Product Manager",
  "profile": "PhD in Organizational Psychology, London Business School. 22 years bridging engineering teams and stakeholders. Former VP Product at two publicly traded technology companies. Pioneer of adaptive roadmapping under uncertainty.\n\nExpertise: ...\n\nResponsibilities: ...\n\nWorks with: ...\n\nDoes not: ...\n\nCommunication style: ..."
}
```

**Resolution semantics:** `role_name` is a string. At bootstrap, Memex mode calls `memex:core:register-role(name, description)` which returns the int `role_id` PK; the loader caches `{role_name → role_id}` and resolves each agent's `role_name` before calling `memex:core:register-agent(agent_id, name, role_id, profile)`. Local mode does the same against the project-local `roles` table. The JSON files store the human-readable name (stable across DBs) rather than an int (unstable across reseeds).

- [ ] **Step 4: Append the agent loader to `scripts/seed_data.py`**

```python
# scripts/seed_data.py — REPLACE the "# Agent loader added by Task 4." sentinel with:

def load_agent_seed() -> list[dict]:
    """Return Atelier's shipped agent profiles as a list of dicts with
    keys: agent_id, name, role_name, profile.

    Iterates templates/agents/*.json in lexicographic order. Callers must
    resolve role_name → role_id via the roles table AFTER bootstrap has
    registered roles (Memex returns role_id from register-role; Local
    selects MAX(id) after the INSERT).
    """
    agents_dir = _TEMPLATES / "agents"
    profiles: list[dict] = []
    for path in sorted(agents_dir.glob("*.json")):
        profiles.append(json.loads(path.read_text(encoding="utf-8")))
    return profiles
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/test_seed_agents.py -v
```
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add templates/agents/ scripts/seed_data.py tests/test_seed_agents.py
git commit -m "feat(seed): wave-0 Atelier agent profile seed + loader"
```

---

### Task 5: Migrations split — shared/ + local-only/ (v1.1.0 clean schema)

**Files:**
- Create directories: `migrations/shared/`, `migrations/local-only/`
- Delete: every existing v1.0.13 migration (`migrations/001_initial_schema.sql` through `migrations/005_soft_walls.sql`)
- Create: `migrations/shared/001_v110_schema.sql` (single file, full v1.1.0 DDL + inline phase seed + `index_id` columns + all indexes)
- Create: `migrations/local-only/050_local_roles_agents.sql`
- Verify: `scripts/migrate.py` already accepts `migrations_dir` (it does at `scripts/migrate.py:9`)
- Test: `tests/test_migration_split.py`

Per spec §11.1, v1.1.0 is a major version bump with a brand-new bootstrap path: there is no v1.0.13 → v1.1.0 in-place ALTER chain. v1.0.13 migrations are deleted; the v1 schema knowledge survives only inside `scripts/migrate_to_memex.py`'s legacy reader (a Plan 4 deliverable — not in Plan 1's scope).

The `index_id` columns and indexes are **inline** in `001_v110_schema.sql`; there is no separate `006_index_ids.sql` because v1.1.0 ships clean.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_migration_split.py
import sqlite3
from pathlib import Path
import pytest
from scripts.migrate import apply_migrations

MIGRATIONS = Path(__file__).parent.parent / "migrations"


def test_shared_directory_exists():
    assert (MIGRATIONS / "shared").is_dir()


def test_local_only_directory_exists():
    assert (MIGRATIONS / "local-only").is_dir()


def test_v1_migrations_deleted():
    """v1.0.13 migrations are deleted per spec §11.1 — only the v1.1.0
    single-file migration remains under shared/."""
    for legacy in ("001_initial_schema.sql", "002_sessions.sql",
                   "003_phases.sql", "004_tasks_parallel.sql",
                   "005_soft_walls.sql"):
        assert not (MIGRATIONS / legacy).exists(), \
            f"v1.0.13 migration {legacy} must be deleted"
        assert not (MIGRATIONS / "shared" / legacy).exists(), \
            f"v1.0.13 migration {legacy} must not be moved into shared/"


def test_shared_has_single_v110_schema_file():
    files = sorted((MIGRATIONS / "shared").glob("*.sql"))
    assert len(files) == 1, \
        f"shared/ should hold exactly one file (001_v110_schema.sql), got {[f.name for f in files]}"
    assert files[0].name == "001_v110_schema.sql"


def test_shared_migration_does_not_define_roles_or_agents():
    f = MIGRATIONS / "shared" / "001_v110_schema.sql"
    text = f.read_text(encoding="utf-8")
    assert "CREATE TABLE roles" not in text and "CREATE TABLE IF NOT EXISTS roles" not in text, \
        "shared schema must not define roles table (lives in agents.db / local-only)"
    assert "CREATE TABLE agents" not in text and "CREATE TABLE IF NOT EXISTS agents" not in text, \
        "shared schema must not define agents table"


def test_local_only_migration_defines_roles_and_agents():
    f = MIGRATIONS / "local-only" / "050_local_roles_agents.sql"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "CREATE TABLE roles" in text
    assert "CREATE TABLE agents" in text


def test_apply_shared_only_to_fresh_db(tmp_path):
    """Memex-mode bootstrap supplies only shared/."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "roles" not in tables
    assert "agents" not in tables
    assert "workspaces" in tables
    assert "projects" in tables
    assert "tasks" in tables
    assert "sessions" in tables
    assert "phases" in tables
    assert "meeting_minutes" in tables


def test_apply_shared_then_local_to_fresh_db(tmp_path):
    """Local-mode bootstrap supplies both directories in order."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "local-only")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "roles" in tables
    assert "agents" in tables
    assert "projects" in tables


def test_apply_migrations_is_idempotent(tmp_path):
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "shared")  # second call must no-op
    con = sqlite3.connect(str(db))
    applied = con.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert applied == 1  # exactly one shared migration


def test_index_id_columns_present_in_shared_schema(tmp_path):
    """index_id columns are inline in 001_v110_schema.sql (no separate
    006 migration). Locks the spec §11.2 contract."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    for table in ("projects", "project_documents", "meeting_minutes", "tasks"):
        cols = [r[1] for r in con.execute(
            f"PRAGMA table_info({table})").fetchall()]
        assert "index_id" in cols, f"{table} missing index_id column"


def test_phases_seeded_from_shared(tmp_path):
    """The phase seed is inlined in 001_v110_schema.sql; a fresh DB has
    the full catalog after shared/ is applied."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    count = con.execute("SELECT COUNT(*) FROM phases").fetchone()[0]
    assert count >= 6, f"expected at least 6 phases seeded inline, got {count}"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_migration_split.py -v
```
Expected: FAIL — directories don't exist; v1 migration files still present.

- [ ] **Step 3: Delete v1.0.13 migrations**

```bash
git rm migrations/001_initial_schema.sql
git rm migrations/002_sessions.sql
git rm migrations/003_phases.sql
git rm migrations/004_tasks_parallel.sql
git rm migrations/005_soft_walls.sql
```

Their v1 schema content survives only inside `scripts/migrate_to_memex.py`'s legacy reader, which is a Plan 4 deliverable. Plan 1 must not implement that script — just delete the migrations.

- [ ] **Step 4: Create `migrations/shared/001_v110_schema.sql`**

Single file with the full v1.1.0 DDL per spec §11.2. Structure:

```sql
-- migrations/shared/001_v110_schema.sql
-- Atelier v1.1.0 schema. Clean redesign for Memex-v2 integration.
--
-- This is THE schema. There is no v1.0.13 → v1.1.0 ALTER path; the
-- migration replay reads v1 rows via scripts.migrate_to_memex's legacy
-- reader and translates them into this layout.
--
-- Both Memex-mode bootstrap (via memex:core:create-store) and Local-mode
-- setup consume this file. Local mode additionally consumes
-- migrations/local-only/050_local_roles_agents.sql.

PRAGMA foreign_keys = ON;

-- workspaces (NEW in v1.1.0; spec §11.2)
CREATE TABLE workspaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    identity    TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_workspaces_identity ON workspaces(identity);

-- projects (workspace_id + slug + index_id added; repo dropped)
CREATE TABLE projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    slug         TEXT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT,
    phase        TEXT NOT NULL DEFAULT 'design:open',
    created_by   TEXT NOT NULL,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(workspace_id, slug)
);
CREATE INDEX idx_projects_workspace ON projects(workspace_id);
CREATE INDEX idx_projects_phase     ON projects(phase);
CREATE INDEX idx_projects_index_id  ON projects(index_id);

-- project_documents (type dropped; domain/subdomain/workspace_id/index_id added)
CREATE TABLE project_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    domain       TEXT NOT NULL,
    subdomain    TEXT,
    title        TEXT NOT NULL,
    filename     TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_docs_workspace  ON project_documents(workspace_id);
CREATE INDEX idx_docs_project    ON project_documents(project_id);
CREATE INDEX idx_docs_domain     ON project_documents(domain);
CREATE INDEX idx_docs_subdomain  ON project_documents(subdomain);
CREATE INDEX idx_docs_index_id   ON project_documents(index_id);

-- tasks (subdomain/claimed_at/completed_at/index_id added)
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    title        TEXT NOT NULL,
    description  TEXT,
    subdomain    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    priority     INTEGER DEFAULT 0,
    notes        TEXT,
    created_by   TEXT NOT NULL,
    assigned_to  TEXT,
    claimed_at   TEXT,
    completed_at TEXT,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_tasks_project    ON tasks(project_id);
CREATE INDEX idx_tasks_status     ON tasks(status);
CREATE INDEX idx_tasks_assigned   ON tasks(assigned_to);
CREATE INDEX idx_tasks_subdomain  ON tasks(subdomain);
CREATE INDEX idx_tasks_index_id   ON tasks(index_id);

-- meeting_minutes (workspace_id added NOT NULL; project_id now nullable; subdomain/index_id added)
CREATE TABLE meeting_minutes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    project_id   INTEGER REFERENCES projects(id),
    title        TEXT NOT NULL,
    date         TEXT NOT NULL,
    subdomain    TEXT,
    filename     TEXT,
    summary      TEXT,
    decisions    TEXT,
    created_by   TEXT NOT NULL,
    index_id     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_meetings_workspace ON meeting_minutes(workspace_id);
CREATE INDEX idx_meetings_project   ON meeting_minutes(project_id);
CREATE INDEX idx_meetings_date      ON meeting_minutes(date);
CREATE INDEX idx_meetings_subdomain ON meeting_minutes(subdomain);
CREATE INDEX idx_meetings_index_id  ON meeting_minutes(index_id);

CREATE TABLE meeting_participants (
    meeting_id INTEGER NOT NULL REFERENCES meeting_minutes(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    PRIMARY KEY (meeting_id, agent_id)
);

-- sessions (workspace_id added NOT NULL)
CREATE TABLE sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id       INTEGER NOT NULL REFERENCES workspaces(id),
    project_id         INTEGER NOT NULL REFERENCES projects(id),
    agent_id           TEXT NOT NULL,
    phase              TEXT,
    pre_diagnose_phase TEXT,
    current_tasks      TEXT,
    accomplished       TEXT,
    next_action        TEXT,
    status             TEXT NOT NULL DEFAULT 'in-progress'
                          CHECK(status IN ('in-progress', 'blocked', 'complete')),
    blocking_reason    TEXT,
    pm_notes           TEXT,
    opened_at          TEXT,
    closed_at          TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sessions_workspace ON sessions(workspace_id);
CREATE INDEX idx_sessions_project   ON sessions(project_id);
CREATE INDEX idx_sessions_agent     ON sessions(agent_id);
CREATE INDEX idx_sessions_status    ON sessions(status);

-- Phase machine — static catalog (identical layout to v1.0.13; seed inlined below)
CREATE TABLE phases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    skill           TEXT NOT NULL,
    state           TEXT NOT NULL,
    description     TEXT NOT NULL,
    is_terminal     BOOLEAN NOT NULL DEFAULT FALSE,
    allow_from_any  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE phase_transitions (
    from_phase TEXT NOT NULL REFERENCES phases(name),
    to_phase   TEXT NOT NULL REFERENCES phases(name),
    PRIMARY KEY (from_phase, to_phase)
);

CREATE TABLE skill_gates (
    skill          TEXT PRIMARY KEY,
    required_phase TEXT REFERENCES phases(name)
);

CREATE TABLE phase_bypasses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    from_phase  TEXT NOT NULL,
    to_phase    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_bypasses_project ON phase_bypasses(project_id);

-- Phase seed (full 19-phase catalog + transitions + skill_gates).
-- Copy verbatim from v1.0.13's 003_phases.sql (the existing file before deletion).
-- This is THE source of truth in v1.1.0; the v1 file is gone after Step 3.
INSERT OR IGNORE INTO phases (name, skill, state, description, is_terminal, allow_from_any) VALUES
    ('design:open',     'dev:design',   'open',     'Grilling and drafting in progress',         0, 0),
    ('design:approved', 'dev:design',   'approved', 'Design document approved by user',          0, 0),
    ('plan:open',       'dev:plan',     'open',     'Implementation plan being written',         0, 0),
    ('plan:approved',   'dev:plan',     'approved', 'Plan approved, ready for TDD',              0, 0),
    ('tdd:red',         'dev:tdd',      'red',      'Failing tests written',                     0, 0),
    ('tdd:green',       'dev:tdd',      'green',    'Tests passing with minimal implementation', 0, 0);
    -- ... full 19-row seed; copy verbatim from the v1.0.13 003_phases.sql
    --     file before deletion (Step 3). Plan execution must preserve the
    --     exact rows + INSERT OR IGNORE phase_transitions + INSERT OR IGNORE
    --     skill_gates blocks.
```

The plan executor must copy the full `INSERT OR IGNORE` block (phases + phase_transitions + skill_gates) **verbatim** from the v1.0.13 `003_phases.sql` file before deleting it in Step 3. This is mechanical — no semantic changes.

- [ ] **Step 5: Create the local-only migration**

```sql
-- migrations/local-only/050_local_roles_agents.sql
-- Local mode owns its own roles + agents tables; Memex mode defers to
-- ~/.memex/agents.db (spec §6.5). Schema mirrors what agents.db exposes
-- so business logic does not branch on mode.

CREATE TABLE roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE agents (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role_id    INTEGER NOT NULL REFERENCES roles(id),
    profile    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 6: Verify `scripts/migrate.py:apply_migrations` already accepts `migrations_dir`**

Confirm with `grep -n "def apply_migrations" scripts/migrate.py` — current signature at line 9 is `def apply_migrations(db_path: str, migrations_dir: str | Path) -> None`. No change needed.

- [ ] **Step 7: Run the new test file to verify it passes**

```
pytest tests/test_migration_split.py -v
```
Expected: 11 passed.

- [ ] **Step 8: Update existing test fixtures to point at `shared/` + `local-only/`**

The legacy fixtures call `apply_migrations(path, MIGRATIONS_DIR)` where `MIGRATIONS_DIR` was the flat `migrations/` directory. In v1.1.0 they must apply both:

```python
apply_migrations(path, MIGRATIONS_DIR / "shared")
apply_migrations(path, MIGRATIONS_DIR / "local-only")
```

Files to grep + update: `grep -lE "apply_migrations.*MIGRATIONS_DIR" tests/`.

Note: existing tests that reference v1 schema columns now removed by v1.1.0 (`projects.repo`, `project_documents.type`) will fail. This is expected; those tests are rewired by Wave 1' (Plan 3) and Wave 1 (Plan 2). Plan 1 only restores fixture green for tests whose surface didn't change (roles + agents seeds, mode detector, facade skeleton, domain vocabulary).

- [ ] **Step 9: Run the full suite, accept known-failing legacy tests**

```
pytest tests/
```
Expected: Plan-1-touched tests pass; v1-schema-column tests in `test_projects.py`, `test_documents.py`, `test_tasks.py`, `test_meetings.py` may fail because their fixtures hardcode v1.0.13 columns (`projects.repo`, `project_documents.type`). Document each failure as deferred to Plan 2/Plan 3. Do **not** rewrite those test bodies in Plan 1.

- [ ] **Step 10: Commit**

```bash
git add migrations/ tests/test_migration_split.py
git commit -m "feat(migrations): wave-0 v1.1.0 clean schema — shared/ + local-only/ split"
```

---

### Task 6: Atelier domain vocabulary doc + constants

**Files:**
- Create: `internal/memex/domain-vocabulary.md`
- Create: `scripts/domain_vocabulary.py`
- Test: `tests/test_domain_vocabulary.py`

Spec §6.4 fixes Atelier's two-level taxonomy:

- **`DOMAINS`** — a 9-entry frozenset (cross-plugin, stable) validated on every Tier 2 write.
- **`SUBDOMAINS`** — a dict-of-lists (Atelier-internal, soft-validated; documents the canonical set per domain).
- **`TYPE_TO_DOMAIN`** — a dict mapping v1.0.13's free-form `project_documents.type` column to v1.1.0's `(domain, subdomain)` pair. Consumed by Plan 4's legacy reader (`scripts/migrate_to_memex.py`).

The markdown file documents the rationale + addition policy for future contributors.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_domain_vocabulary.py
from pathlib import Path
import pytest
from scripts import domain_vocabulary as dv


# ── DOMAINS ─────────────────────────────────────────────────────────────

def test_canonical_domains_present():
    expected = {"project", "task", "meeting", "design", "adr",
                "research", "postmortem", "log", "project_doc"}
    assert expected <= dv.DOMAINS


def test_domains_is_frozenset_of_nine():
    assert isinstance(dv.DOMAINS, frozenset)
    assert len(dv.DOMAINS) == 9, f"expected 9 domains per spec §6.4, got {len(dv.DOMAINS)}"


def test_assert_valid_accepts_every_domain():
    for d in dv.DOMAINS:
        dv.assert_valid(d)  # must not raise


def test_assert_valid_rejects_unknown():
    with pytest.raises(ValueError, match="unknown domain"):
        dv.assert_valid("blog_post")


# ── SUBDOMAINS ──────────────────────────────────────────────────────────

def test_subdomains_is_dict_keyed_by_domain():
    assert isinstance(dv.SUBDOMAINS, dict)
    # Every key in SUBDOMAINS must be a known domain.
    for d in dv.SUBDOMAINS:
        assert d in dv.DOMAINS, f"SUBDOMAINS references unknown domain {d!r}"


def test_subdomains_cover_documented_domains():
    """Spec §6.4 lines 359-368 lists subdomains for 7 of the 9 domains
    (project and adr are atomic — no subdomains)."""
    for d in ("task", "meeting", "design", "research", "postmortem", "log", "project_doc"):
        assert d in dv.SUBDOMAINS, f"{d} missing subdomain catalog"
        assert isinstance(dv.SUBDOMAINS[d], (list, tuple, frozenset))
        assert len(dv.SUBDOMAINS[d]) > 0


def test_subdomain_specific_canonical_values():
    """Spot-check a few canonical entries from spec §6.4."""
    assert "standup" in dv.SUBDOMAINS["meeting"]
    assert "bug" in dv.SUBDOMAINS["task"]
    assert "api" in dv.SUBDOMAINS["design"]
    assert "plan" in dv.SUBDOMAINS["project_doc"]


# ── TYPE_TO_DOMAIN ──────────────────────────────────────────────────────

def test_type_to_domain_returns_domain_subdomain_pairs():
    assert isinstance(dv.TYPE_TO_DOMAIN, dict)
    for v1_type, mapped in dv.TYPE_TO_DOMAIN.items():
        assert isinstance(mapped, tuple) and len(mapped) == 2
        domain, subdomain = mapped
        assert domain in dv.DOMAINS, \
            f"TYPE_TO_DOMAIN[{v1_type!r}] domain {domain!r} not in DOMAINS"


def test_type_to_domain_covers_known_v1_types():
    """The mapping must handle v1.0.13's stable type values. Unknown
    values fall back to (project_doc, <type>) at the call site, not here."""
    for v1_type in ("design", "plan", "adr", "research", "postmortem"):
        assert v1_type in dv.TYPE_TO_DOMAIN, \
            f"v1 type {v1_type!r} missing from TYPE_TO_DOMAIN"


# ── Doc ─────────────────────────────────────────────────────────────────

def test_vocabulary_doc_exists():
    f = Path(__file__).parent.parent / "internal" / "memex" / "domain-vocabulary.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    for d in ("project", "task", "meeting", "design", "adr",
              "research", "postmortem", "log", "project_doc"):
        assert d in text, f"doc missing domain {d!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_domain_vocabulary.py -v
```

- [ ] **Step 3: Create `scripts/domain_vocabulary.py`**

```python
# scripts/domain_vocabulary.py
"""Atelier's domain vocabulary for Tier 2 writes through Memex Index.

Memex doesn't enforce a domain enum — Atelier owns the small, stable set.
Adding a new domain is a deliberate spec revision, not an inline call.
See internal/memex/domain-vocabulary.md for the policy.

Three constants live here:

- DOMAINS:        frozenset of 9 strings; cross-plugin enforcement.
- SUBDOMAINS:     dict {domain: [stable subdomains]}; Atelier-internal,
                  soft-validated (unknown subdomain values accepted).
- TYPE_TO_DOMAIN: dict {v1_type_string: (domain, subdomain)}; consumed
                  by scripts/migrate_to_memex.py's legacy reader (Plan 4).
"""
from __future__ import annotations


# ── DOMAINS (spec §6.4, lines 343-353) ──────────────────────────────────

DOMAINS: frozenset[str] = frozenset({
    "project",       # atelier.db.projects rows
    "task",          # atelier.db.tasks rows
    "meeting",       # atelier.db.meeting_minutes rows
    "design",        # project_documents subset — system/feature designs
    "adr",           # project_documents subset — architecture decision records
    "research",      # project_documents subset — reference + evaluation notes
    "postmortem",    # project_documents subset — incident/release/retro write-ups
    "log",           # project_documents subset (or workspace-level) — journals
    "project_doc",   # project_documents catch-all (plans, runbooks, release notes)
})


# ── SUBDOMAINS (spec §6.4, lines 359-368) ───────────────────────────────

SUBDOMAINS: dict[str, list[str]] = {
    "task":        ["bug", "feature", "chore", "spike", "refactor"],
    "meeting":     ["standup", "design-review", "retro", "1-1",
                    "customer", "incident", "kickoff", "planning"],
    "design":      ["api", "data", "infra", "ux", "security", "migration"],
    "research":    ["evaluation", "reference", "summary", "comparison"],
    "postmortem":  ["incident", "release", "retro"],
    "log":         ["daily", "decision", "lesson"],
    "project_doc": ["plan", "runbook", "release-notes", "pr-description"],
    # "project" and "adr" are atomic — no subdomains.
}


# ── TYPE_TO_DOMAIN (spec §11.4) ─────────────────────────────────────────
# Maps v1.0.13's free-form project_documents.type column to v1.1.0's
# (domain, subdomain) pair. Used by scripts/migrate_to_memex.py's
# legacy reader (Plan 4). Unknown v1 types fall back to
# ("project_doc", <type>) at the call site — see spec §11.4 pseudocode.

TYPE_TO_DOMAIN: dict[str, tuple[str, str | None]] = {
    # Promoted to first-class domains in v1.1.0
    "design":          ("design",     None),
    "adr":             ("adr",        None),
    "research":        ("research",   None),
    "postmortem":      ("postmortem", None),
    "log":             ("log",        None),
    # Stay under project_doc with a meaningful subdomain
    "plan":            ("project_doc", "plan"),
    "runbook":         ("project_doc", "runbook"),
    "release-notes":   ("project_doc", "release-notes"),
    "pr-description":  ("project_doc", "pr-description"),
    "notes":           ("project_doc", None),
    "spec":            ("design",     None),    # historical alias
}


def assert_valid(domain: str) -> None:
    """Hard-validate a domain string against the v1 vocabulary.

    Subdomains are NOT validated here — see SUBDOMAINS for the soft
    canonical set; callers may pass any string. Spec §6.4 documents the
    rationale ("subdomain enforcement is soft").
    """
    if domain not in DOMAINS:
        raise ValueError(
            f"unknown domain {domain!r}; valid Atelier domains: "
            f"{sorted(DOMAINS)}. Adding one requires a spec amendment "
            f"(see internal/memex/domain-vocabulary.md)."
        )
```

- [ ] **Step 4: Create `internal/memex/domain-vocabulary.md`**

```markdown
# Atelier domain vocabulary (Memex Index)

Atelier-side rows in `~/.memex/index.db.documents.domain` use this fixed
vocabulary when written via Tier 2 (caller-built `librarian_output`).
Memex does not enforce a domain enum — this list IS the enforcement,
maintained by Atelier and validated by `scripts.domain_vocabulary.assert_valid()`.

## Current vocabulary (9 domains, spec §6.4)

| Domain | Atelier source table | Promotion rationale |
|---|---|---|
| `project`     | `projects`           | Top-level work efforts; cross-project recall ("what projects have I run"). |
| `task`        | `tasks`              | Atomic work items; cross-project recall ("what bugs did I fix last quarter"). |
| `meeting`     | `meeting_minutes`    | Decisions/discussions cross-cut projects. |
| `design`      | `project_documents` (subset) | Patterns recur across projects — "every auth design I've drafted". |
| `adr`         | `project_documents` (subset) | High-value cross-project lookup is the canonical ADR use case. |
| `research`    | `project_documents` (subset) | Tech-topic recall ("notes on Postgres tuning across all projects"). |
| `postmortem`  | `project_documents` (subset) | Lessons cross-cut by failure mode, not project. |
| `log`         | `project_documents` (subset, or workspace-level) | Time-bounded recall; often workspace- or human-scoped. |
| `project_doc` | `project_documents` (catch-all) | Generic bucket for typed-but-not-promoted docs (e.g., `plan`, `runbook`). |

`plan` deliberately does NOT get its own domain — plans are project-bound
and rarely useful cross-project. They ride under `project_doc` with
`subdomain="plan"`.

## Subdomain vocabulary (Atelier-internal, soft-validated)

| Domain | Stable subdomains |
|---|---|
| `task`        | `bug`, `feature`, `chore`, `spike`, `refactor` |
| `meeting`     | `standup`, `design-review`, `retro`, `1-1`, `customer`, `incident`, `kickoff`, `planning` |
| `design`      | `api`, `data`, `infra`, `ux`, `security`, `migration` |
| `research`    | `evaluation`, `reference`, `summary`, `comparison` |
| `postmortem`  | `incident`, `release`, `retro` |
| `log`         | `daily`, `decision`, `lesson` |
| `project_doc` | `plan`, `runbook`, `release-notes`, `pr-description`, free-form |
| `project`, `adr` | (no subdomains — atomic) |

Subdomain enforcement is **soft** — unknown values are accepted; the
list above documents the canonical set per domain. Drift is acceptable;
a future audit can promote stable additions.

## Legacy type → (domain, subdomain) mapping

`scripts.domain_vocabulary.TYPE_TO_DOMAIN` translates v1.0.13's free-form
`project_documents.type` strings into the v1.1.0 two-level taxonomy.
Consumed by `scripts/migrate_to_memex.py`'s legacy reader (Plan 4).
Unknown v1 types fall back to `("project_doc", <type>)` at the call site.

## Addition policy

- **Adding a domain** — spec amendment; update `DOMAINS` frozenset; add test coverage.
- **Adding a subdomain** — Atelier-internal; update `SUBDOMAINS[domain]` list; PR comment justifying the addition.

The friction on domains exists because cross-plugin search relies on
stable strings. Adding a domain that overlaps with Memex Brain's own
taxonomy (`article`, `capture`, `synthesis`) would muddle
`memex:brain:ask` results. Worth the spec round-trip.
```

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/test_domain_vocabulary.py -v
```
Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/domain_vocabulary.py internal/memex/domain-vocabulary.md tests/test_domain_vocabulary.py
git commit -m "feat(domain-vocab): wave-0 9-domain set + subdomains + legacy type map"
```

---

### Task 7: Workspace resolution helper (`workspace_root()` + `find_git_root()`)

**Files:**
- Modify: `scripts/git_utils.py` (append `find_git_root` + `git_remote_url`)
- Modify: `scripts/workspace.py` (append `workspace_root()` — the existing module is the tmux helper; we append, we do NOT replace)
- Test: `tests/test_workspace_root.py`

**Why this task exists (cross-plan dependency):**
Plan 3 Task 1 (`scripts/documents.py` rewrite) imports `from scripts.workspace import workspace_root` and uses it to resolve the on-disk filename of a project document before passing the body to `backend.write_document`. Spec §6.8's pseudocode (line 487) writes the same call. Without this helper, every Plan 3 `create_document` call site fails at import time. The matching `find_git_root` helper is referenced by spec §10.2 `resolve_scope()` pseudocode (line 674) but never defined in the current `scripts/git_utils.py`.

**Module-placement note:** Plan 3's actual import line is `from scripts.workspace import workspace_root` (singular). The existing `scripts/workspace.py` is the tmux session helper — we **append** `workspace_root()` to that module rather than create a new file, so Plan 3's import resolves without a rename. (Spec §10.2 originally described `scripts/scope.py`; that file is still introduced later for `resolve_scope()` and the per-workspace session state, but the unqualified `workspace_root()` helper lives in `scripts/workspace.py` because that's where Plan 3 reads it from.)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_workspace_root.py
import os
import subprocess
from pathlib import Path
import pytest


def _make_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_find_git_root_returns_root_from_root(tmp_path):
    _make_git_repo(tmp_path / "repo")
    from scripts.git_utils import find_git_root
    assert find_git_root(tmp_path / "repo") == (tmp_path / "repo").resolve()


def test_find_git_root_returns_root_from_subdir(tmp_path):
    _make_git_repo(tmp_path / "repo")
    sub = tmp_path / "repo" / "src" / "deep"
    sub.mkdir(parents=True)
    from scripts.git_utils import find_git_root
    assert find_git_root(sub) == (tmp_path / "repo").resolve()


def test_find_git_root_returns_none_outside_repo(tmp_path):
    # tmp_path is not under any git repo (pytest tmp_path is a fresh tree)
    outside = tmp_path / "no-repo"
    outside.mkdir()
    from scripts.git_utils import find_git_root
    assert find_git_root(outside) is None


def test_workspace_root_returns_git_root(tmp_path, monkeypatch):
    _make_git_repo(tmp_path / "repo")
    monkeypatch.chdir(tmp_path / "repo")
    from scripts.workspace import workspace_root
    assert workspace_root() == (tmp_path / "repo").resolve()


def test_workspace_root_raises_outside_git(tmp_path, monkeypatch):
    outside = tmp_path / "no-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    from scripts.workspace import workspace_root
    with pytest.raises(FileNotFoundError, match="not inside a git repository"):
        workspace_root()
```

- [ ] **Step 2: Run tests — expect failure (missing helpers)**

```
pytest tests/test_workspace_root.py -v
```

- [ ] **Step 3: Append to `scripts/git_utils.py`**

```python
# scripts/git_utils.py — APPEND

def find_git_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default CWD) until a directory containing
    a `.git` entry (file or directory) is found. Returns the resolved
    path of that directory, or None if the walk hits filesystem root.

    Used by `scripts.workspace.workspace_root()` and `scripts.scope.
    resolve_scope()` (spec §10.2). A `.git` *file* (not directory)
    indicates a submodule or worktree — both still resolve to a valid
    workspace per the spec's workspace-identity rules.
    """
    cur = Path(start).resolve() if start else Path.cwd().resolve()
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return None


def git_remote_url(root: Path) -> str | None:
    """Return `origin`'s push URL for the repo at `root`, or None if
    there is no remote configured. Used by spec §10.2's workspace
    identity rule (`identity = git_remote_url(root) or str(root)`).
    """
    res = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=False,
    )
    url = res.stdout.strip()
    return url or None
```

- [ ] **Step 4: Append to `scripts/workspace.py`**

```python
# scripts/workspace.py — APPEND (does NOT replace the existing tmux helpers)

from pathlib import Path
from scripts.git_utils import find_git_root


def workspace_root() -> Path:
    """Resolve the workspace root (= git root) for the current process.

    Plan 3's `scripts/documents.py:create_document` uses this to
    locate the on-disk markdown file at `workspace_root() / filename`
    before passing its body to `backend.write_document` (spec §6.8).

    Raises FileNotFoundError when CWD isn't inside a git repository —
    workspace-less callers (rare; mainly daily-log writes) must catch
    and skip the workspace-bound path, or operate under explicit
    `workspace_id=None` semantics per spec §10.4.
    """
    cwd = Path.cwd().resolve()
    root = find_git_root(cwd)
    if root is None:
        raise FileNotFoundError(
            f"not inside a git repository: {cwd}. "
            f"workspace_root() requires CWD to be under a git workspace."
        )
    return root
```

- [ ] **Step 5: Run tests — expect pass (5 tests)**

```
pytest tests/test_workspace_root.py -v
```

- [ ] **Step 6: Commit**

```bash
git add scripts/git_utils.py scripts/workspace.py tests/test_workspace_root.py
git commit -m "feat(workspace): wave-0 workspace_root + find_git_root helpers (Plan 3 dep)"
```

---

## Wave 0 acceptance

| # | Check | Pass criterion |
|---|---|---|
| 1 | Task 1 — facade | `scripts/backend.py` declares all 17 methods from spec §4.3 plus three cross-plan helpers (`lookup_index_id_by_source_ref`, `find_or_create_role`, `find_or_create_agent`) — 20 total; every call raises `NotImplementedError`; signatures are keyword-callable. |
| 2 | Task 2 — mode detector | `detect_mode()` returns `"local"` when `~/.memex/registry.json` absent or pinned plugin unreachable; `"memex"` only when both present + manifest validated; result cached. |
| 3 | Task 3 — roles seed | `templates/roles.json` exists; `load_role_seed()` returns ≥46 dicts; PM canonical name is "Product Manager"; parity with `scripts/seed_roles.py:ROLES` enforced by test. |
| 4 | Task 4 — agents seed | `templates/agents/<agent_id>.json` exists for every entry in `scripts/seed_roles.py:ROLES`; `load_agent_seed()` returns ≥46 dicts; every `role_name` resolves in `roles.json`. |
| 5 | Task 5 — migrations | v1.0.13 migrations deleted; `migrations/shared/001_v110_schema.sql` is the single shared file; `migrations/local-only/050_local_roles_agents.sql` contains roles + agents only; `index_id` columns + phase seed inline in shared. |
| 6 | Task 6 — domain vocab | `DOMAINS` frozenset of 9 strings; `SUBDOMAINS` dict covers 7 documented domains; `TYPE_TO_DOMAIN` covers v1.0.13's known type values; `assert_valid` enforced. |
| 7 | Task 7 — workspace_root | `scripts.git_utils.find_git_root` + `scripts.workspace.workspace_root` exist and resolve to the git root; raise `FileNotFoundError` outside a repo. |

Hand-off: Wave 1 (Plan 2) reads `backend.py` signatures and replaces `NotImplementedError` bodies with Memex-dispatched implementations using `librarian.write_entry` directly + caller-built `librarian_output` per spec §6.2. Wave 1' (Plan 3) does the same with Local SQLite. Wave 2 (Plan 4) ships the legacy reader in `scripts/migrate_to_memex.py` that consumes `domain_vocabulary.TYPE_TO_DOMAIN`.
