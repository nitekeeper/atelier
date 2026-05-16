# Atelier ↔ Memex v2 Retrofit — Plan 1 of 4: Foundations (Wave 0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay down the scaffolding Wave 1 / Wave 1' / Wave 2 will build on — persistence-facade skeleton, mode detector, seed data, and a clean split between migrations that ship to Memex Core vs. migrations local-mode only consumes.

**Architecture:** This wave produces only the contracts and scaffolding. No business logic is rewired yet. Every task in this plan is independent and can be dispatched in **one parallel batch** of 5 subagents.

**Tech Stack:** Python 3.10+, pytest, sqlite3 (stdlib), pathlib, JSON for seed data.

**Spec reference:** [docs/specs/2026-05-16-atelier-memex-v2-retrofit-design.md](../specs/2026-05-16-atelier-memex-v2-retrofit-design.md) §§4-5, §11.

---

## Parallel dispatch map

```
Wave 0 — all parallel, 5 independent tasks
┌────────────────┐ ┌────────────────┐ ┌─────────────────┐ ┌────────────────┐ ┌─────────────────┐
│ Task 1         │ │ Task 2         │ │ Task 3          │ │ Task 4         │ │ Task 5          │
│ backend.py     │ │ mode_detector  │ │ roles seed JSON │ │ agents seed    │ │ migrations split│
│ facade skeleton│ │ + tests        │ │ + loader        │ │ JSON + loader  │ │ shared/local-only│
└────────────────┘ └────────────────┘ └─────────────────┘ └────────────────┘ └─────────────────┘
```

All 5 tasks touch disjoint files. Dispatch all five as parallel subagents per `superpowers:dispatching-parallel-agents`.

---

### Task 1: Persistence facade skeleton

**Files:**
- Create: `scripts/backend.py`
- Test: `tests/test_backend_skeleton.py`

The facade defines the API surface that Wave 1 (Memex) and Wave 1' (Local) will both implement. In Wave 0 it raises `NotImplementedError` for every method but its signature is the contract. Mode-routing comes from Task 2.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backend_skeleton.py
"""Wave 0 contract tests for the persistence facade.

Each method must exist with the expected signature and raise
NotImplementedError when called. Wave 1 / Wave 1' replace the bodies.
"""
import pytest
import inspect
from scripts import backend


def test_facade_module_exists():
    assert hasattr(backend, "write_document")
    assert hasattr(backend, "write_task")
    assert hasattr(backend, "write_meeting")
    assert hasattr(backend, "upsert_session")
    assert hasattr(backend, "transition_phase")
    assert hasattr(backend, "update_task_status")
    assert hasattr(backend, "record_phase_bypass")
    assert hasattr(backend, "find_documents")
    assert hasattr(backend, "get_task")
    assert hasattr(backend, "list_tasks")
    assert hasattr(backend, "find_project_by_key")


@pytest.mark.parametrize("fn_name,kwargs", [
    ("write_document", dict(domain="design", title="t", body="b", metadata={}, caller_agent_id="a")),
    ("write_task", dict(title="t", description="d", project_id=1, created_by="a")),
    ("write_meeting", dict(title="t", date="2026-05-16", summary="s", decisions="d", created_by="a")),
    ("upsert_session", dict(project_id=1, agent_id="a", phase="design:open")),
    ("transition_phase", dict(project_id=1, to_phase="plan:open", agent_id="a")),
    ("update_task_status", dict(task_id=1, status="in-progress")),
    ("record_phase_bypass", dict(project_id=1, from_phase="x", to_phase="y", reason="r", agent_id="a")),
    ("find_documents", dict(query="q")),
    ("get_task", dict(task_id=1)),
    ("list_tasks", dict(project_id=1)),
    ("find_project_by_key", dict(project_key="abc")),
])
def test_method_raises_not_implemented(fn_name, kwargs):
    fn = getattr(backend, fn_name)
    with pytest.raises(NotImplementedError):
        fn(**kwargs)


def test_methods_accept_keyword_args_only():
    """Wave 1 / 1' will swap implementations; keyword-only signatures
    prevent positional-arg drift between backends."""
    for name in ("write_document", "write_task", "write_meeting",
                 "upsert_session", "transition_phase", "find_documents"):
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
the two backends as they evolve.
"""
from __future__ import annotations
from typing import Any


def _not_implemented(name: str) -> None:
    raise NotImplementedError(
        f"backend.{name} has no implementation yet. "
        f"Wave 1 (Memex) or Wave 1' (Local) supplies the body."
    )


def write_document(*, domain: str, title: str, body: str,
                   metadata: dict, caller_agent_id: str,
                   source_url: str | None = None) -> dict:
    _not_implemented("write_document")


def write_task(*, title: str, description: str, project_id: int,
               created_by: str, assigned_to: str | None = None,
               priority: int = 0, notes: str | None = None) -> dict:
    _not_implemented("write_task")


def write_meeting(*, title: str, date: str, summary: str,
                  decisions: str, created_by: str,
                  project_id: int | None = None) -> dict:
    _not_implemented("write_meeting")


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


def find_documents(*, query: str, project_id: int | None = None,
                   domain: str | None = None, limit: int = 10) -> list[dict]:
    _not_implemented("find_documents")


def get_task(*, task_id: int) -> dict | None:
    _not_implemented("get_task")


def list_tasks(*, project_id: int, status: str | None = None) -> list[dict]:
    _not_implemented("list_tasks")


def find_project_by_key(*, project_key: str) -> dict | None:
    _not_implemented("find_project_by_key")
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_backend_skeleton.py -v
```
Expected: 14 passed.

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


def _memex_plugin_reachable() -> bool:
    """True if a Memex plugin install can be found in Claude Code's
    plugin cache and its plugin.json parses.

    We check the canonical agora marketplace cache path. Future variants
    may also live under a user-installed plugin dir; that's a future
    refinement (it would be a new fallback path here)."""
    base = Path.home() / ".claude" / "plugins" / "cache" / "agora" / "memex"
    if not base.exists():
        return False
    # Find any versioned subdirectory with a parseable plugin.json
    for v_dir in base.iterdir():
        manifest = v_dir / ".claude-plugin" / "plugin.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data.get("name") == "memex":
                    return True
            except (json.JSONDecodeError, OSError):
                continue
    return False


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
Expected: 6 passed.

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

This packages Atelier's role catalog (PM, Architect, Engineer, Tech Writer, QA, Designer) as data, so both bootstrap paths (Memex `register-role` and Local `roles` INSERT) consume the same source of truth.

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
    assert len(roles) >= 6, "must include at least PM, Architect, Engineer, Tech Writer, QA, Designer"
    for r in roles:
        assert {"name", "description"} <= r.keys()


def test_role_seed_has_canonical_atelier_roles():
    roles = load_role_seed()
    names = {r["name"] for r in roles}
    assert {"Project Manager", "Software Architect", "Software Engineer",
            "Tech Writer", "QA", "Designer"} <= names


def test_role_seed_names_are_unique():
    roles = load_role_seed()
    names = [r["name"] for r in roles]
    assert len(names) == len(set(names))
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_seed_roles.py -v -k "role_seed"
```
Expected: FAIL — file missing, loader missing.

- [ ] **Step 3: Create the seed file**

```json
{
  "roles": [
    {
      "name": "Project Manager",
      "description": "Owns scope, sequencing, and inter-agent dispatch. Translates user intent into a phase-gated workflow; surfaces blockers; never writes code."
    },
    {
      "name": "Software Architect",
      "description": "Owns system-level design decisions. Writes ADRs, evaluates schema and API trade-offs, signs off on cross-cutting changes before implementation begins."
    },
    {
      "name": "Software Engineer",
      "description": "Implements features against approved plans. Practices strict TDD: failing test, minimal code, passing test, commit."
    },
    {
      "name": "Tech Writer",
      "description": "Owns user-facing documentation. Drafts and revises README, CHANGELOG, user guides, and skill descriptions to match shipped behavior."
    },
    {
      "name": "QA",
      "description": "Owns test strategy and coverage gaps. Writes integration and end-to-end tests; verifies acceptance criteria against shipped behavior; reports regressions."
    },
    {
      "name": "Designer",
      "description": "Owns user-experience design and information architecture for any user-visible surface (CLI flows, skill prompts, error messages)."
    }
  ]
}
```

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
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add templates/roles.json scripts/seed_data.py tests/test_seed_roles.py
git commit -m "feat(seed): wave-0 Atelier role seed JSON + loader"
```

---

### Task 4: Atelier shipped agents seed + loader

**Files:**
- Create: `templates/agents/pm.json`
- Create: `templates/agents/architect.json`
- Create: `templates/agents/engineer.json`
- Create: `templates/agents/tech_writer.json`
- Create: `templates/agents/qa.json`
- Create: `templates/agents/designer.json`
- Modify: `scripts/seed_data.py` (append agent loader; non-conflicting region)
- Create: `tests/test_seed_agents.py`

This task is independent of Task 3 in terms of files **except** for `scripts/seed_data.py`. The append region is below a sentinel comment Task 3 leaves in place (`# Agent loader added by Task 4.`). The two subagents must not edit each other's regions. If running them in parallel, Task 4 appends; Task 3 writes the file with the sentinel.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_agents.py
from pathlib import Path
import json
from scripts.seed_data import load_agent_seed

TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "agents"
EXPECTED_AGENT_FILES = ["pm.json", "architect.json", "engineer.json",
                       "tech_writer.json", "qa.json", "designer.json"]


def test_agent_seed_files_exist():
    for f in EXPECTED_AGENT_FILES:
        assert (TEMPLATES_DIR / f).exists(), f"missing {f}"


def test_load_agent_seed_returns_six_agents():
    agents = load_agent_seed()
    assert len(agents) == 6


def test_each_agent_has_required_fields():
    for a in load_agent_seed():
        assert {"agent_id", "name", "role_name", "profile"} <= a.keys()
        assert isinstance(a["profile"], str) and len(a["profile"]) > 100


def test_agent_ids_unique():
    agents = load_agent_seed()
    ids = [a["agent_id"] for a in agents]
    assert len(ids) == len(set(ids))


def test_agent_role_names_match_role_seed():
    from scripts.seed_data import load_role_seed
    role_names = {r["name"] for r in load_role_seed()}
    for a in load_agent_seed():
        assert a["role_name"] in role_names, \
            f"agent {a['agent_id']} references unknown role {a['role_name']}"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_seed_agents.py -v
```
Expected: FAIL — agent files + loader missing.

- [ ] **Step 3: Create six agent profile JSON files**

```json
// templates/agents/pm.json
{
  "agent_id": "atelier-pm-1",
  "name": "Project Manager (Atelier)",
  "role_name": "Project Manager",
  "profile": "Senior project manager specialized in human-AI software development teams. 15 years orchestrating cross-functional engineering work; deep experience translating fuzzy user intent into concrete, phase-gated work plans. Operates the Atelier phase machine — design → plan → tdd:red/green → review → ship — and never permits work to begin without explicit user approval on the prior phase. Dispatches subagents in parallel when their tasks touch disjoint files; sequential when they share state. Refuses to write code; refuses to make architectural decisions. Surfaces blockers immediately; never optimizes for the appearance of progress."
}
```

```json
// templates/agents/architect.json
{
  "agent_id": "atelier-architect-1",
  "name": "Software Architect (Atelier)",
  "role_name": "Software Architect",
  "profile": "PhD in distributed systems with 20 years designing service architectures and data-storage substrates. Authority on schema-evolution discipline, idempotent migration design, eventual consistency under federation, and the trade-offs between centralized and per-store ownership. Writes Architecture Decision Records (ADRs) before any non-trivial implementation; evaluates schema/API/protocol choices against explicit acceptance criteria; signs off on cross-cutting changes only after the trade-offs are written down. Refuses architectural decisions made implicitly by code reviewer comments. Refuses to merge a change that violates a stated invariant; surfaces the conflict and requires explicit override."
}
```

```json
// templates/agents/engineer.json
{
  "agent_id": "atelier-engineer-1",
  "name": "Software Engineer (Atelier)",
  "role_name": "Software Engineer",
  "profile": "Senior software engineer with 12 years shipping production code in Python, TypeScript, and Rust. Practices strict test-driven development: write the failing test, run it to confirm it fails, write the minimal code to make it pass, run it to confirm green, then commit. Refuses to commit unverified work. Refuses to leave broken tests in a feature branch. Refactors in commits separate from feature commits. Writes minimal code by default — YAGNI — and resists the urge to add structure for hypothetical future requirements. Comments only when the why is non-obvious; identifiers carry the what."
}
```

```json
// templates/agents/tech_writer.json
{
  "agent_id": "atelier-tech-writer-1",
  "name": "Tech Writer (Atelier)",
  "role_name": "Tech Writer",
  "profile": "Veteran technical writer with 18 years documenting developer-facing products. Owns README, CHANGELOG, user guides, and skill descriptions. Reads shipped code before writing docs; never describes behavior that doesn't exist. Writes for the reader who has no prior context. Prefers concrete examples over abstract description. Updates CHANGELOG on every user-visible change. Drafts release notes in the imperative voice. Surfaces inconsistencies between code and docs as bugs in either; never silently 'fixes' docs to match unintended behavior."
}
```

```json
// templates/agents/qa.json
{
  "agent_id": "atelier-qa-1",
  "name": "QA (Atelier)",
  "role_name": "QA",
  "profile": "Senior quality engineer with 14 years owning test strategy across embedded, web, and CLI products. Owns coverage strategy; reads every feature spec for testable behavior; writes integration and end-to-end tests that exercise the actual ship surface. Refuses to gate releases on unit-test coverage alone. Writes acceptance tests against the spec, not against the implementation. Surfaces regressions as P0 bugs; refuses to mark them 'expected'. Knows the difference between 'works on my machine' and 'verified in CI'."
}
```

```json
// templates/agents/designer.json
{
  "agent_id": "atelier-designer-1",
  "name": "Designer (Atelier)",
  "role_name": "Designer",
  "profile": "Senior designer with 16 years shaping developer-facing CLIs, skill prompts, and error messaging. Owns information architecture and user-experience of every interactive surface. Reviews every user-visible string before ship. Refuses to ship error messages that don't tell the user how to fix the problem. Refuses to ship skill descriptions that don't tell the model when to invoke them. Writes copy in the active voice; trims hedge words; prefers nouns and verbs over adjectives."
}
```

- [ ] **Step 4: Append the agent loader to `scripts/seed_data.py`**

```python
# scripts/seed_data.py — REPLACE the "# Agent loader added by Task 4." sentinel with:

def load_agent_seed() -> list[dict]:
    """Return Atelier's shipped agent profiles as a list of dicts with
    keys: agent_id, name, role_name, profile."""
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
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add templates/agents/ scripts/seed_data.py tests/test_seed_agents.py
git commit -m "feat(seed): wave-0 Atelier agent profile seed + loader"
```

---

### Task 5: Migrations split — shared/ + local-only/

**Files:**
- Create directories: `migrations/shared/`, `migrations/local-only/`
- Move: every file in `migrations/*.sql` into `migrations/shared/` (preserve filenames)
- Create: `migrations/local-only/100_local_roles_agents.sql`
- Modify: `scripts/migrate.py` (parameterize the migrations directory)
- Test: `tests/test_migration_split.py`

Per spec §11, the v2 architecture requires the role/agent CREATE TABLE statements to be Local-mode-only (Memex mode's atelier.db must NOT have these — they live in `~/.memex/agents.db`).

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


def test_shared_migrations_do_not_define_roles_table():
    for f in sorted((MIGRATIONS / "shared").glob("*.sql")):
        text = f.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS roles" not in text, \
            f"{f.name} must not define roles table (shared = Memex-and-Local-compatible)"
        assert "CREATE TABLE IF NOT EXISTS agents" not in text, \
            f"{f.name} must not define agents table"


def test_local_only_migration_defines_roles_and_agents():
    f = MIGRATIONS / "local-only" / "100_local_roles_agents.sql"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS roles" in text
    assert "CREATE TABLE IF NOT EXISTS agents" in text


def test_apply_shared_only_to_fresh_db(tmp_path):
    """Memex-mode bootstrap supplies only shared/."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "roles" not in tables
    assert "agents" not in tables
    assert "projects" in tables
    assert "tasks" in tables
    assert "sessions" in tables


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


def test_apply_migrations_is_idempotent_with_split(tmp_path):
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    apply_migrations(str(db), MIGRATIONS / "shared")  # second call must no-op
    con = sqlite3.connect(str(db))
    applied = con.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert applied == len(list((MIGRATIONS / "shared").glob("*.sql")))
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_migration_split.py -v
```
Expected: FAIL — directories don't exist; existing migrations still define roles/agents in `001_initial_schema.sql`.

- [ ] **Step 3: Split the existing migrations**

```bash
mkdir -p migrations/shared migrations/local-only
git mv migrations/001_initial_schema.sql migrations/shared/001_initial_schema.sql
git mv migrations/002_sessions.sql migrations/shared/002_sessions.sql
git mv migrations/003_phases.sql migrations/shared/003_phases.sql
git mv migrations/004_tasks_parallel.sql migrations/shared/004_tasks_parallel.sql
git mv migrations/005_soft_walls.sql migrations/shared/005_soft_walls.sql
```

- [ ] **Step 4: Strip roles + agents from `migrations/shared/001_initial_schema.sql`**

Open `migrations/shared/001_initial_schema.sql`. Delete the two `CREATE TABLE IF NOT EXISTS roles` and `CREATE TABLE IF NOT EXISTS agents` blocks at the top. Leave everything else.

The remaining shared/001 should start with `CREATE TABLE IF NOT EXISTS projects (` and continue from there. The `projects.created_by TEXT NOT NULL REFERENCES agents(id)` clause becomes `projects.created_by TEXT NOT NULL` (no FK; agents.db is a separate file in Memex mode). Replace `REFERENCES agents(id)` with a comment `-- references agents.id in agents.db or local-only roles+agents` on every line.

- [ ] **Step 5: Create the local-only migration**

```sql
-- migrations/local-only/100_local_roles_agents.sql
-- Local-mode-only: project-local atelier.db needs its own roles + agents
-- tables since there is no ~/.memex/agents.db to defer to. Schema matches
-- what Memex's agents.db exposes so business logic does not branch.

CREATE TABLE IF NOT EXISTS roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role_id     INTEGER NOT NULL REFERENCES roles(id),
    profile     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

- [ ] **Step 6: Update `scripts/migrate.py` to accept a directory parameter**

The current `apply_migrations(db_path, migrations_dir)` already takes the directory — verify with `grep "def apply_migrations" scripts/migrate.py`. If it hardcodes `Path("migrations")`, parameterize it now.

```python
# scripts/migrate.py — relevant function
def apply_migrations(db_path: str, migrations_dir) -> None:
    """Apply all .sql files in migrations_dir in lexicographic order,
    recording each in the `migrations` table for idempotency."""
    migrations_dir = Path(migrations_dir)
    conn = get_connection(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS migrations (
        filename TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )""")
    conn.commit()
    applied = {r[0] for r in conn.execute("SELECT filename FROM migrations").fetchall()}
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        if sql_file.name in applied:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO migrations (filename, applied_at) VALUES (?, datetime('now'))",
                     (sql_file.name,))
        conn.commit()
    conn.close()
```

- [ ] **Step 7: Run tests to verify they pass**

```
pytest tests/test_migration_split.py -v
```
Expected: 7 passed.

- [ ] **Step 8: Re-run the full existing test suite to verify nothing regressed**

```
pytest tests/ -x
```
Expected: ALL existing tests still pass (they will need `apply_migrations(db_path, MIGRATIONS / "shared" / "local-only")` style calls — see Step 9).

- [ ] **Step 9: Update existing test fixtures to pass both dirs**

In every existing test file whose `db_path` fixture calls `apply_migrations(path, MIGRATIONS_DIR)`, change to:

```python
apply_migrations(path, MIGRATIONS_DIR / "shared")
apply_migrations(path, MIGRATIONS_DIR / "local-only")
```

Files to grep: `grep -l "apply_migrations" tests/`. Update each.

- [ ] **Step 10: Re-run full suite**

```
pytest tests/
```
Expected: all green.

- [ ] **Step 11: Add the `index_id` column migration (spec §11.2)**

Create `migrations/shared/006_index_ids.sql`:

```sql
-- migrations/shared/006_index_ids.sql
-- Denormalized linkback to ~/.memex/index.db.documents.index_id.
-- Populated by Memex-mode writes (Wave 1). Stays NULL in Local mode.
-- Nullable so Local-mode writes don't need to fabricate a value.

ALTER TABLE projects           ADD COLUMN index_id TEXT;
ALTER TABLE project_documents  ADD COLUMN index_id TEXT;
ALTER TABLE meeting_minutes    ADD COLUMN index_id TEXT;
ALTER TABLE tasks              ADD COLUMN index_id TEXT;

CREATE INDEX IF NOT EXISTS idx_projects_index_id           ON projects(index_id);
CREATE INDEX IF NOT EXISTS idx_project_documents_index_id  ON project_documents(index_id);
CREATE INDEX IF NOT EXISTS idx_meeting_minutes_index_id    ON meeting_minutes(index_id);
CREATE INDEX IF NOT EXISTS idx_tasks_index_id              ON tasks(index_id);
```

- [ ] **Step 12: Add a test that locks the column into the schema**

Append to `tests/test_migration_split.py`:

```python
def test_index_id_columns_added_to_shared_schema(tmp_path):
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS / "shared")
    con = sqlite3.connect(str(db))
    for table in ("projects", "project_documents", "meeting_minutes", "tasks"):
        cols = [r[1] for r in con.execute(
            f"PRAGMA table_info({table})").fetchall()]
        assert "index_id" in cols, f"{table} missing index_id column"
```

Run: `pytest tests/test_migration_split.py -v` — expect green.

- [ ] **Step 13: Commit**

```bash
git add migrations/ scripts/migrate.py tests/
git commit -m "feat(migrations): wave-0 split into shared/ + local-only/ + index_id columns"
```

---

## Wave 0 acceptance

- All 5 tasks merged.
- `pytest tests/` green.
- `scripts/backend.py` exists with 11 NotImplementedError methods.
- `scripts/mode_detector.py:detect_mode()` is cached + tested.
- `templates/roles.json` and `templates/agents/*.json` exist and load.
- `migrations/shared/` and `migrations/local-only/` exist; no shared migration defines roles or agents.

Hand-off: Wave 1 (Plan 2) reads `backend.py` signatures and replaces NotImplementedError bodies with Memex-dispatched implementations; Wave 1' does the same with Local SQLite.
