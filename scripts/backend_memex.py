# scripts/backend_memex.py
"""Memex-mode backend (Tier 2 caller-built librarian_output path).

Writes through memex:index:write WITHOUT the Librarian LLM dispatch —
Atelier knows its domain, builds the classification deterministically,
and calls librarian.write_entry() directly. See spec §6.2.

Requires Memex v2.2.0+ (the version that ships librarian.validate_output
and the optional librarian_output parameter on memex:index:write).

This module is built in three logical sections matching Plan 2 tasks 1-3:
  1. Document writes (Tier 2): write_document / write_project /
     write_task / write_meeting and the _atelier_write engine.
  2. Operational state writes: upsert_session / transition_phase /
     update_task_status / record_phase_bypass via Memex Core CRUD.
  3. Reads + cross-plan helpers: find_documents / get_task / list_tasks /
     lookup_index_id_by_source_ref / find_or_create_role / find_or_create_agent
     and the raw _memex_core_execute primitive for composite-key DELETE.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from scripts import domain_vocabulary


# ── Memex plugin location + import bootstrap ───────────────────────────────


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
    """Legacy compat hook — historically appended the Memex plugin root
    to `sys.path` so `from scripts.agents import librarian` would resolve
    Memex's package rather than Atelier's `scripts/agents.py` module.

    That approach was broken by design: Python caches the first
    `scripts.agents` import in `sys.modules` (Atelier's single-file
    module loads first, since `from scripts import domain_vocabulary` at
    the top of this file triggers it), so the Memex package was never
    actually reachable through normal imports. Production code now uses
    `_load_memex_module` (file-path-based, bypasses sys.modules) and
    this function is preserved only because several tests monkeypatch
    it to a no-op. New code paths should call `_load_memex_module`.
    """
    p = str(_memex_plugin_root())
    if p not in sys.path:
        sys.path.insert(0, p)


@functools.lru_cache(maxsize=None)
def _load_memex_module(plugin_root: Path, dotted: str) -> ModuleType:
    """Load `<plugin_root>/scripts/<dotted-path>.py` (or the matching
    package `__init__.py`) as an isolated module, sidestepping the
    `sys.modules['scripts.agents']` shadow planted by Atelier's
    `scripts/agents.py`.

    The loaded module is given a synthetic name (`_memex_<dotted>`) so
    it never collides with anything already in `sys.modules`. The
    `lru_cache` decorator keeps each `(plugin_root, dotted)` pair from
    paying the spec/exec cost more than once per process.

    Tests that want to inject a stub module should call
    `_load_memex_module.cache_clear()` and then either pre-populate the
    cache with `cache_setdefault`-style monkeypatching or simply patch
    the attributes on the returned module.
    """
    rel = dotted.replace(".", "/")
    candidates = [
        plugin_root / "scripts" / f"{rel}.py",
        plugin_root / "scripts" / rel / "__init__.py",
    ]
    for path in candidates:
        if path.is_file():
            mod_name = f"_memex_{dotted.replace('.', '_')}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:  # pragma: no cover
                raise ImportError(
                    f"failed to build import spec for {path}")
            module = importlib.util.module_from_spec(spec)
            # Register under the synthetic name BEFORE exec_module so
            # any internal relative imports the package may attempt can
            # find their siblings. We do NOT shadow the real
            # `scripts.<name>` entries.
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError(
        f"memex module {dotted!r} not found under {plugin_root / 'scripts'}")


def _memex_module(dotted: str) -> ModuleType:
    """Thin wrapper that pairs `_memex_plugin_root` with
    `_load_memex_module` so every call site stays one line.
    """
    return _load_memex_module(_memex_plugin_root(), dotted)


# ── Memex Tier 2 thin wrappers (also serve as patch surfaces in tests) ─────


def _memex_validate_output(librarian_output: dict) -> dict:
    """Delegate to Memex's librarian.validate_output."""
    librarian = _memex_module("agents.librarian")
    return librarian.validate_output(librarian_output)


def _memex_write_entry(*, payload: dict, librarian_output: dict,
                       target_store: str, target_table: str,
                       caller_agent_id: str,
                       embedding: bytes | None) -> dict:
    """Delegate to Memex's librarian.write_entry."""
    librarian = _memex_module("agents.librarian")
    return librarian.write_entry(
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
    embeddings = _memex_module("embeddings")
    return embeddings.encode(text)


def _memex_log_embedding_skip(exc, *, caller_agent_id: str,
                              index_id: str, input_chars: int) -> None:
    """Forward to Memex's structured audit log per v2.4.1 contract."""
    embeddings = _memex_module("embeddings")
    embeddings.log_skip(
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
    # Iteration follows dict insertion order (Python 3.7+ guarantee), so
    # callers that care about narrative ordering can control it by the
    # order they assemble the metadata dict.
    for value in metadata.values():
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def _build_key(*, workspace_slug: str, project_slug: str | None,
               domain: str, created_at_iso: str, title: str) -> str:
    """Canonical key per spec §6.7:
    `<workspace_slug>/<project_slug>/<domain>/<date>-<title_slug>-<seq>`.

    Memex v2.3.0+ enforces `UNIQUE` on `documents.key`, so we allocate
    the smallest unused `seq` for the (workspace/project/domain/date/title)
    prefix. Duplicate-key races are handled one layer up in `_atelier_write`.
    """
    date_str = created_at_iso[:10]  # YYYY-MM-DD
    title_slug = _slug(title, max_length=48)
    project_part = project_slug or "(no-project)"
    seq = _next_seq(workspace_slug, project_part, domain, date_str, title_slug)
    return (
        f"{workspace_slug}/{project_part}/{domain}/"
        f"{date_str}-{title_slug}-{seq}"
    )


def _next_seq(workspace_slug: str, project_slug: str, domain: str,
              date_str: str, title_slug: str) -> int:
    """Smallest unused integer ≥ 1 for the (workspace/project/domain/date/title)
    prefix. Runs a `key LIKE prefix%` scan over `index.documents`."""
    memex_stores = _memex_module("stores")
    prefix = (
        f"{workspace_slug}/{project_slug}/{domain}/{date_str}-{title_slug}-"
    )
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


def _try_embed(text: str, *, caller_agent_id: str,
               index_id: str) -> bytes | None:
    """Best-effort embedding.

    Narrows to embeddings.EmbeddingUnavailable per memex v2.4.1 — any
    other exception is a real bug and propagates. On the typed miss,
    forwards to memex's audit log (embeddings.log_skip) so the skip is
    visible in ~/.memex/audits/embedding-skip-log.md, then returns None
    so the write proceeds FTS5-only.

    The type-narrowing import is deferred to the except site so a fully
    stubbed call path (test harnesses) doesn't force a real Memex
    install. The probe is permissive: any exception whose class name is
    `EmbeddingUnavailable` qualifies, falling back to a real isinstance
    check only when needed.
    """
    try:
        return _memex_embed(text)
    except Exception as e:
        if not _is_embedding_unavailable(e):
            raise
        _memex_log_embedding_skip(
            e,
            caller_agent_id=caller_agent_id,
            index_id=index_id,
            input_chars=len(text),
        )
        return None


def _is_embedding_unavailable(exc: BaseException) -> bool:
    """Lazy check: True iff `exc` is Memex's `EmbeddingUnavailable`.

    Same deferred-import pattern as `_is_duplicate_key_error` — keeps
    the happy path free of an unconditional `scripts.embeddings` load,
    which is fragile under the Atelier/Memex namespace collision.
    """
    if type(exc).__name__ == "EmbeddingUnavailable":
        return True
    try:
        memex_embeddings = _memex_module("embeddings")
    except Exception:
        return False
    return isinstance(exc, memex_embeddings.EmbeddingUnavailable)


_WORKSPACE_SLUG = "atelier"  # Single-workspace deployment; spec §6.7.


def _resolve_project_slug(project_id: int | None) -> str | None:
    """Best-effort project_id → project_slug lookup for key construction.

    On miss (no project_id, or row absent) returns None and `_build_key`
    falls back to the `(no-project)` literal. Cheap query — caller is on
    the write path which is already a multi-call sequence."""
    if project_id is None:
        return None
    try:
        memex_stores = _memex_module("stores")
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
        try:
            memex_stores = _memex_module("stores")
            # TODO(multi-workspace): filter by workspace_id when
            # multi-workspace lands. Today _WORKSPACE_SLUG is the only
            # workspace so the project-domain JSON filter is sufficient.
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
                   relations: list[dict] | None,
                   caller_agent_id: str,
                   project_slug_override: str | None = None) -> dict:
    """Tier 2 atelier write — synchronous, no LLM dispatch.

    Builds librarian_output deterministically, validates via Memex, and
    persists via librarian.write_entry. The target row goes into
    ~/.memex/atelier.db.<target_table> with an index_id linkback;
    the matching documents row goes into ~/.memex/index.db.

    Per spec §6.7 + §6.8:
    - `key` is `<workspace>/<project>/<domain>/<date>-<title>-<seq>` (UNIQUE)
    - `searchable` is `title + body + metadata_narrative` (no truncation cap)

    `project_slug_override` lets `write_project` plant its OWN slug as
    the project slot of the key (it has no `project_id` row yet, since
    it IS the project row). Other callers fall back to
    `_resolve_project_slug(metadata.project_id)`.
    """
    domain_vocabulary.assert_valid(domain)

    created_at = _now()
    if project_slug_override is not None:
        project_slug = project_slug_override
    else:
        project_slug = _resolve_project_slug(
            (metadata or {}).get("project_id"))
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

    # Defer the librarian import until we actually need the exception
    # class. Eagerly importing it here would force ~/.memex/config.json
    # to exist even when callers have stubbed `_memex_write_entry` — and
    # would collide with Atelier's own `scripts/agents.py` module on the
    # import path. The deferred path runs only on the DuplicateKeyError
    # branch, which test harnesses set up with their own fake librarian.
    try:
        return _attempt(key)
    except Exception as exc:
        if not _is_duplicate_key_error(exc):
            raise
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


def _is_duplicate_key_error(exc: BaseException) -> bool:
    """Test for `librarian.DuplicateKeyError` without an unconditional
    import at module load.

    Memex's librarian raises a dedicated `DuplicateKeyError` (see
    memex/scripts/agents/librarian.py:50). We can't `except` it by name
    without first importing — but that import is fragile under the
    Atelier/Memex `scripts.agents` namespace collision (Atelier's
    `scripts/agents.py` shadows Memex's `scripts.agents/` package on
    sys.path). Best to look up the class lazily AT the except site,
    inside a broad `except` that re-raises anything else.
    """
    # First the structural check: any exception whose qualified name
    # ends in DuplicateKeyError counts, irrespective of module path. This
    # is robust to test stubs that register their own DuplicateKeyError
    # class without collision-proofing.
    if type(exc).__name__ == "DuplicateKeyError":
        return True
    # Fall back to a lazy load — only reached when the type-name probe
    # fails (e.g. some future librarian rename). If the load itself
    # fails, we conservatively report False so the original exception
    # propagates unaltered.
    try:
        memex_librarian = _memex_module("agents.librarian")
    except Exception:
        return False
    return isinstance(exc, memex_librarian.DuplicateKeyError)


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
    """Persist a project document via Memex Tier 2.

    Validates `domain` against `scripts.domain_vocabulary.DOMAINS` BEFORE
    any Memex call, so the unknown-domain path never opens
    ~/.memex/config.json (callers see a clean `ValueError`).

    `source_url` (when supplied) is folded into `metadata["source_url"]`
    so it persists in `~/.memex/index.db.documents.metadata` alongside
    other narrative fields and contributes to the FTS5 searchable blob
    via `_metadata_narrative`.
    """
    # Hard-validate first — keeps the validation path hermetic.
    domain_vocabulary.assert_valid(domain)
    target_table = _DOMAIN_TO_TABLE.get(domain) or "project_documents"
    metadata = dict(metadata or {})
    if source_url:
        metadata["source_url"] = source_url
    now = _now()
    payload = {
        "title": title,
        "filename": metadata.get("filename", _slug(title) + ".md"),
        "project_id": metadata.get("project_id"),
        "type": domain,
        "created_by": caller_agent_id,
        "created_at": now,
        "updated_at": now,
    }
    return _atelier_write(
        target_table=target_table, domain=domain,
        title=title, body=body, payload=payload,
        metadata=metadata, relations=relations,
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
    now = _now()
    payload = {
        "title": title, "description": description, "project_id": project_id,
        "created_by": created_by, "assigned_to": assigned_to,
        "priority": priority, "notes": notes, "status": "pending",
        "created_at": now, "updated_at": now,
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
    now = _now()
    payload = {
        "title": title, "date": date,
        "filename": f"{date}-{_slug(title)}.md",
        "summary": summary, "decisions": decisions,
        "created_by": created_by,
        "created_at": now, "updated_at": now,
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
    later by `_resolve_project_slug` for key construction.

    Passes its own `slug` as the project slot of the new document's
    canonical key (rather than the `(no-project)` literal) since a
    project row IS its own project parent.
    """
    now = _now()
    payload = {
        "workspace_id": workspace_id,
        "slug": slug,
        "name": name,
        "description": description,
        "phase": "design:open",
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
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
        project_slug_override=slug,
    )


# ════════════════════════════════════════════════════════════════════════════
# Section 2: Operational state writes (Plan 2 Task 2)
# ════════════════════════════════════════════════════════════════════════════
#
# Tier 1 writes — direct Memex Core CRUD, no Librarian dispatch. These
# rows are operational state (sessions, phase bypasses, status flips)
# that don't carry searchable narrative, so they bypass index.documents
# entirely and live only in atelier.db.<table>.


# ── Memex Core CRUD helpers ────────────────────────────────────────────────
#
# Route through memex.stores' public API rather than hand-built SQL so we
# benefit from `safe_identifier` validation and don't reintroduce
# SQL-injection risk. `insert` returns the inserted row; `update` returns
# the updated row; `query` is SELECT-only (no commit).


def _memex_core_insert(*, store: str, table: str, row: dict) -> dict:
    """Insert `row` into `<store>.<table>` via Memex Core; returns the
    inserted row dict (including server-assigned id / timestamps)."""
    memex_stores = _memex_module("stores")
    return memex_stores.insert(store, table, row)


def _memex_core_update(*, store: str, table: str, row_id: int,
                      changes: dict) -> dict:
    """Apply `changes` to the row with `id = row_id` in `<store>.<table>`
    via Memex Core; returns the updated row dict (or `{}` if Core
    returned None, which means the row vanished mid-update)."""
    memex_stores = _memex_module("stores")
    updated = memex_stores.update(store, table, row_id, changes)
    return updated or {}


def _memex_core_query(*, store: str, table: str,
                     where: dict | None = None) -> list[dict]:
    """Read-side helper. Builds a simple equality WHERE clause; column
    names are pinned to safe identifiers by callers (no user-controlled
    column names reach here). Defensive: passes `table` through
    `memex_stores.safe_identifier` so a stray bad identifier surfaces as
    a clean ValueError rather than an interpolated-SQL surprise."""
    memex_stores = _memex_module("stores")
    safe_table = memex_stores.safe_identifier(table)
    if where:
        clauses = " AND ".join(f"{k} = ?" for k in where)
        sql = f"SELECT * FROM {safe_table} WHERE {clauses}"
        return memex_stores.query(store, sql, tuple(where.values()))
    return memex_stores.query(store, f"SELECT * FROM {safe_table}", ())


# ── Operational state writes ───────────────────────────────────────────────


def upsert_session(*, project_id: int, agent_id: str,
                   phase: str | None = None,
                   current_tasks: str | None = None,
                   accomplished: str | None = None,
                   next_action: str | None = None,
                   status: str = "in-progress",
                   pm_notes: str | None = None) -> dict:
    """Idempotent session row for (project_id, agent_id).

    Looks up the open-status row for the (project_id, agent_id) pair; if
    one exists we UPDATE only the supplied fields (None values are
    skipped so callers can do incremental updates), otherwise we INSERT
    a new row.

    Per spec §11.1 the `sessions` table carries (project_id, agent_id,
    phase, current_tasks, accomplished, next_action, status, pm_notes,
    created_at, updated_at). created_at/updated_at are filled by the
    table's DEFAULT clauses; we don't override.
    """
    existing = _memex_core_query(store="atelier", table="sessions",
                                 where={"project_id": project_id,
                                        "agent_id": agent_id,
                                        "status": "in-progress"})
    payload = {
        "phase": phase, "current_tasks": current_tasks,
        "accomplished": accomplished, "next_action": next_action,
        "status": status, "pm_notes": pm_notes,
    }
    # Drop unset fields so callers can do incremental updates.
    payload = {k: v for k, v in payload.items() if v is not None}
    if existing:
        return _memex_core_update(store="atelier", table="sessions",
                                  row_id=existing[0]["id"],
                                  changes=payload)
    payload.update({"project_id": project_id, "agent_id": agent_id})
    return _memex_core_insert(store="atelier", table="sessions",
                              row=payload)


def transition_phase(*, project_id: int, to_phase: str,
                     agent_id: str,
                     bypass_reason: str | None = None) -> dict:
    """Advance projects.phase.

    `bypass_reason` is accepted for facade-signature parity with
    backend.py and IS IGNORED here. Callers MUST invoke
    `record_phase_bypass` BEFORE calling `transition_phase` to log the
    audit trail. Passing `bypass_reason` here without first calling
    `record_phase_bypass` silently loses audit data — this ordering is
    deliberate so a transient failure between the two writes leaves a
    coherent trail (bypass-logged-but-not-transitioned is a soft state
    we can detect; transitioned-but-not-logged would drop the bypass
    record silently)."""
    rows = _memex_core_query(store="atelier", table="projects",
                             where={"id": project_id})
    if not rows:
        raise ValueError(f"project_id {project_id} not found")
    return _memex_core_update(store="atelier", table="projects",
                              row_id=project_id,
                              changes={"phase": to_phase})


def update_task_status(*, task_id: int, status: str,
                       notes: str | None = None) -> dict:
    """Set tasks.status; optionally append notes. Future enhancement
    (Plan 3) will write status-derived timestamps (claimed_at,
    completed_at) when those columns are reachable through the
    backend's CRUD surface."""
    changes: dict = {"status": status}
    if notes:
        changes["notes"] = notes
    return _memex_core_update(store="atelier", table="tasks",
                              row_id=task_id, changes=changes)


def record_phase_bypass(*, project_id: int, from_phase: str,
                        to_phase: str, reason: str,
                        agent_id: str) -> dict:
    """Log a soft-wall bypass to atelier.db.phase_bypasses. Surfaced by
    internal/dev-handoff retros so the team can audit how often soft
    walls were crossed and whether the policy needs tightening."""
    return _memex_core_insert(
        store="atelier", table="phase_bypasses",
        row={
            "project_id": project_id,
            "from_phase": from_phase,
            "to_phase": to_phase,
            "reason": reason,
            "agent_id": agent_id,
        },
    )


# ════════════════════════════════════════════════════════════════════════════
# Section 3: Reads + cross-plan helpers (Plan 2 Task 3)
# ════════════════════════════════════════════════════════════════════════════
#
# find_documents goes through Memex's reference librarian (FTS5-only;
# we skip the LLM dispatch). get_task / list_tasks are simple Core reads.
#
# The cross-plan helpers (source_ref lookup, idempotent role/agent,
# raw execute) belong here because they're built on the same plumbing
# as the reads, and Plans 3/4 reference them directly.


# ── Reads ──────────────────────────────────────────────────────────────────


def _memex_search(*, query: str, project_id: int | None = None,
                  domain: str | None = None,
                  workspace_id: int | None = None,
                  subdomain: str | None = None,
                  limit: int = 10) -> list[dict]:
    """Run an FTS5-only Memex Index search by calling the Reference
    Librarian's `execute_query_plan` directly. We skip the subagent step
    (`ask_prepare` builds an LLM prompt we'd never dispatch), so the
    prep helper is dead code on this path. Brain-style ask/synthesize
    (with the subagent loop) still go via `memex:run`.

    Memex's `reference_librarian.execute_query_plan` honors only
    `domain` and `store` filters; everything else (`project_id`,
    `workspace_id`, `subdomain`) is post-filtered here by reading
    `metadata.<field>` off each returned row. We over-fetch by 4x when a
    post-filter is in effect so the caller still gets `limit` results
    after pruning, then truncate.
    """
    memex_ref = _memex_module("agents.reference_librarian")
    needs_post_filter = (project_id is not None
                         or workspace_id is not None
                         or subdomain is not None)
    plan: dict = {
        "fts_query": query,
        "vector_query": None,
        "filters": {},
        "limit": limit * 4 if needs_post_filter else limit,
    }
    if domain:
        plan["filters"]["domain"] = domain
    raw = memex_ref.execute_query_plan(plan, with_embedding=False)
    if not needs_post_filter:
        return raw

    def _meta(row: dict) -> dict:
        md = row.get("metadata")
        if isinstance(md, str):
            try:
                return json.loads(md)
            except json.JSONDecodeError:
                return {}
        return md or {}

    results: list[dict] = []
    for row in raw:
        md = _meta(row)
        if project_id is not None and md.get("project_id") != project_id:
            continue
        if workspace_id is not None and md.get("workspace_id") != workspace_id:
            continue
        if subdomain is not None and md.get("subdomain") != subdomain:
            continue
        results.append(row)
        if len(results) >= limit:
            break
    return results


def find_documents(*, query: str, workspace_id: int | None = None,
                   project_id: int | None = None,
                   domain: str | None = None,
                   subdomain: str | None = None,
                   limit: int = 10) -> list[dict]:
    """FTS5 search over the Memex index. `workspace_id`, `project_id`,
    and `subdomain` are honored via post-filtering against the
    JSON-encoded `metadata` field on each row, since Memex's
    `execute_query_plan` only natively understands `domain` / `store`
    filters today. `domain` rides the native plan filter.
    """
    return _memex_search(query=query, project_id=project_id,
                         domain=domain, workspace_id=workspace_id,
                         subdomain=subdomain, limit=limit)


def get_task(*, task_id: int) -> dict | None:
    """Read a single task row by id. Returns None on miss."""
    rows = _memex_core_query(store="atelier", table="tasks",
                             where={"id": task_id})
    return rows[0] if rows else None


def list_tasks(*, project_id: int,
               status: str | None = None) -> list[dict]:
    """List tasks for a project, optionally filtered by status."""
    where: dict = {"project_id": project_id}
    if status:
        where["status"] = status
    return _memex_core_query(store="atelier", table="tasks", where=where)


# ── Cross-plan helpers ─────────────────────────────────────────────────────


def lookup_index_id_by_source_ref(*, source_ref: str) -> str | None:
    """Look up the `index_id` of a previously-written document whose
    `metadata.source_ref` equals `source_ref`. Returns None if absent.

    Plan 4 contract: `migrate_to_memex.py` calls this before every
    replay write so a rerun after a partial outage skips rows that
    already landed in Memex (avoiding `librarian.DuplicateKeyError`).
    Source refs are stable strings like `"atelier:tasks:42"`.
    """
    memex_stores = _memex_module("stores")
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
    memex_registry = _memex_module("registry")
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
    memex_roles = _memex_module("roles")
    db_path = _agents_db_path()
    for r in memex_roles.list_roles(db_path):
        if r["name"] == name:
            return r
    return memex_roles.create_role(db_path, name=name,
                                    description=description)


def find_or_create_agent(*, agent_id: str, name: str, role_id: int,
                         profile: str) -> dict:
    """Return the agent row with this `agent_id`, creating it if absent.

    Idempotent — symmetric to `find_or_create_role`. Memex's
    `scripts.agents.create_agent` signature is
    `(db_path, agent_id, name, role_id, profile)` per
    `memex/scripts/agents/__init__.py:26`.
    """
    agents_pkg = _memex_module("agents")
    db_path = _agents_db_path()
    existing = agents_pkg.get_agent(db_path, agent_id)
    if existing is not None:
        return existing
    return agents_pkg.create_agent(db_path, agent_id, name, role_id,
                                    profile)


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
    memex_registry = _memex_module("registry")
    memex_db = _memex_module("db")
    rec = memex_registry.get_store(store)
    if rec is None:
        raise ValueError(f"Unknown store: {store}")
    conn = memex_db.get_connection(rec["path"])
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
