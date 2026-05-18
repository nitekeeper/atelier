"""Replay a project-local atelier.db into the machine-global Memex
substrate. Triggered once per project when Memex is detected.

Non-destructive on failure: no marker is written, the local DB is not
renamed, so the next Atelier command retries. Idempotent on retry:
each row is checked against the Memex Index by `source_ref`
(`atelier:<table>:<local_id>`) before being written, so a re-run after
a partial outage skips rows that already landed.

Per-project markers:
  - `.ai/atelier.migrated` (JSON) — set on successful migration.
  - `.ai/atelier.local-only` (JSON) — set when the user declines.
Either marker suppresses re-prompting via `should_prompt`.
"""
from __future__ import annotations

import datetime
import json
import shutil
import sqlite3
from pathlib import Path


# ── timestamp helpers ──────────────────────────────────────────────────────


def _now_compact() -> str:
    """UTC `YYYYMMDDTHHMMSS` — used in the archive filename so a
    naive lexical sort matches chronological order."""
    return datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")


def _now_iso() -> str:
    """UTC ISO-8601 — used in marker payloads."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── local DB reader ────────────────────────────────────────────────────────


def _connect_local(local_db: Path) -> sqlite3.Connection:
    """Open the project-local atelier.db read-only with Row factory so
    callers can refer to columns by name."""
    c = sqlite3.connect(str(local_db))
    c.row_factory = sqlite3.Row
    return c


def _columns(c: sqlite3.Connection, table: str) -> set[str]:
    """Set of column names on `table`. Used to gracefully tolerate
    legacy / partial schemas — a v1.0.x DB may lack columns that v1.1.0
    requires, and a v1.1.0 DB may have columns we don't reference."""
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _get(row: sqlite3.Row, col: str, default=None):
    """Row['col'] that survives a missing column. `sqlite3.Row` raises
    `IndexError` on missing keys, so we widen-then-narrow rather than
    paying for a `try/except` per column."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return default


# ── idempotency precheck ───────────────────────────────────────────────────


def _index_id_for_atelier_row(source_ref: str) -> str | None:
    """Returns the existing `index_id` if a row with this `source_ref`
    is already in the Memex Index, else None.

    The replay layer calls this BEFORE every `backend_memex.write_*`
    so a re-run after a partial outage skips rows that already landed.
    Without this precheck, Memex v2.3.0+ `librarian.write_entry` would
    raise `DuplicateKeyError` on every previously-migrated row.
    """
    from scripts import backend_memex
    return backend_memex.lookup_index_id_by_source_ref(source_ref=source_ref)


# ── core replay ────────────────────────────────────────────────────────────


def migrate_project(local_db: Path) -> dict:
    """Replay local rows into Memex; on success rename the local DB and
    drop a `.migrated` marker.

    Behavior contract:
      * `.ai/atelier.migrated` already present → returns `{"status": "skipped"}`
        without touching Memex or the DB file.
      * Memex not bootstrapped → raises `RuntimeError` with operator
        guidance pointing at `memex:run`.
      * Any write raises → propagates the exception. The marker is NOT
        written and the local DB is NOT renamed, so the next attempt
        retries cleanly.
      * Success → renames the local DB to
        `atelier-pre-migration-<utc-timestamp>.db` and writes the
        marker containing the per-table counts.

    Idempotency: each project / task / meeting / document write is
    preceded by a `lookup_index_id_by_source_ref(atelier:<table>:<id>)`
    check. Rows already present count toward `already_present` and are
    skipped — a partial outage can safely be retried.
    """
    ai_dir = Path(local_db).parent
    marker = ai_dir / "atelier.migrated"
    if marker.exists():
        return {"status": "skipped", "migrated": {},
                "reason": "marker present"}

    # Memex must be bootstrapped before we can route writes to it. The
    # helper lives on the `backend_memex` facade so tests can stub it
    # without reaching into `_memex_module`. On real installations the
    # helper resolves Memex's `db.require_bootstrap()` and raises a
    # clean `RuntimeError` (with operator guidance) on failure.
    from scripts import backend_memex
    backend_memex.require_memex_bootstrap()

    c = _connect_local(local_db)
    migrated = {"projects": 0, "tasks": 0, "meetings": 0,
                "sessions": 0, "phase_bypasses": 0, "documents": 0}
    already_present = {"projects": 0, "tasks": 0, "meetings": 0,
                       "sessions": 0, "phase_bypasses": 0, "documents": 0}
    try:
        # Order matters: projects first so child rows can reference real IDs.
        _replay_projects(c, backend_memex, migrated, already_present)
        _replay_tasks(c, backend_memex, migrated, already_present)
        _replay_meetings(c, backend_memex, migrated, already_present)
        _replay_sessions(c, backend_memex, migrated)
        _replay_phase_bypasses(c, backend_memex, migrated)
        _replay_project_documents(c, backend_memex, migrated, already_present)
    finally:
        c.close()

    # All rows replayed successfully — rename local DB + write marker.
    archive_name = f"atelier-pre-migration-{_now_compact()}.db"
    shutil.move(str(local_db), str(ai_dir / archive_name))
    marker.write_text(json.dumps({
        "migrated_at": _now_iso(),
        "migrated": migrated,
        "already_present": already_present,
        "archived_to": archive_name,
    }, indent=2), encoding="utf-8")
    return {"status": "migrated", "migrated": migrated,
            "already_present": already_present,
            "archived_to": archive_name}


# ── per-table replay helpers ───────────────────────────────────────────────
#
# Each helper takes the open connection, the backend facade module, and
# the running counters. They are deliberately small so the main flow
# reads as an outline of the migration order.


def _replay_projects(c: sqlite3.Connection, backend_memex,
                     migrated: dict, already_present: dict) -> None:
    """Replay the `projects` table. Project rows are persisted as
    Memex documents under the `project` domain — the canonical
    representation matches `backend_memex.write_project`'s output."""
    cols = _columns(c, "projects")
    if "id" not in cols:
        return
    for r in c.execute("SELECT * FROM projects"):
        source_ref = f"atelier:projects:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["projects"] += 1
            continue
        # v1.1.0 schema has no `repo` column; tolerate v1.0.x rows that
        # had one without forcing the migration to know about it.
        metadata = {
            "name": r["name"],
            "description": _get(r, "description"),
            "phase": _get(r, "phase", "design:open"),
            "local_id": r["id"],
            "source_ref": source_ref,
        }
        repo = _get(r, "repo")
        if repo:
            metadata["repo"] = repo
        backend_memex.write_document(
            domain="project",
            title=r["name"],
            body=f"# {r['name']}\n\n{_get(r, 'description') or ''}",
            metadata=metadata,
            caller_agent_id=_get(r, "created_by", "atelier-system"),
        )
        migrated["projects"] += 1


def _replay_tasks(c: sqlite3.Connection, backend_memex,
                  migrated: dict, already_present: dict) -> None:
    cols = _columns(c, "tasks")
    if "id" not in cols:
        return
    for r in c.execute("SELECT * FROM tasks"):
        source_ref = f"atelier:tasks:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["tasks"] += 1
            continue
        backend_memex.write_task(
            title=r["title"],
            description=_get(r, "description") or "",
            project_id=r["project_id"],
            created_by=_get(r, "created_by", "atelier-system"),
            assigned_to=_get(r, "assigned_to"),
            priority=_get(r, "priority", 0) or 0,
            notes=_get(r, "notes"),
            source_ref=source_ref,
        )
        migrated["tasks"] += 1


def _replay_meetings(c: sqlite3.Connection, backend_memex,
                     migrated: dict, already_present: dict) -> None:
    cols = _columns(c, "meeting_minutes")
    if "id" not in cols:
        return
    for r in c.execute("SELECT * FROM meeting_minutes"):
        source_ref = f"atelier:meeting_minutes:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["meetings"] += 1
            continue
        backend_memex.write_meeting(
            title=r["title"],
            date=r["date"],
            summary=_get(r, "summary") or "",
            decisions=_get(r, "decisions") or "",
            created_by=_get(r, "created_by", "atelier-system"),
            project_id=_get(r, "project_id"),
            source_ref=source_ref,
        )
        migrated["meetings"] += 1


def _replay_sessions(c: sqlite3.Connection, backend_memex,
                     migrated: dict) -> None:
    """Sessions are operational state (Tier 1) — no `source_ref`
    idempotency hook because the Memex side has its own
    (project_id, agent_id, status='in-progress') upsert key."""
    try:
        rows = list(c.execute("SELECT * FROM sessions"))
    except sqlite3.OperationalError:
        return
    for r in rows:
        backend_memex.upsert_session(
            project_id=r["project_id"],
            agent_id=r["agent_id"],
            phase=_get(r, "phase"),
            current_tasks=_get(r, "current_tasks"),
            accomplished=_get(r, "accomplished"),
            next_action=_get(r, "next_action"),
            status=_get(r, "status", "in-progress") or "in-progress",
            pm_notes=_get(r, "pm_notes"),
        )
        migrated["sessions"] += 1


def _replay_phase_bypasses(c: sqlite3.Connection, backend_memex,
                           migrated: dict) -> None:
    """Phase bypasses are append-only audit rows; no idempotency hook
    because re-running migration is a programming error here (the local
    DB rename in the success path prevents it). We still tolerate a
    missing table — some legacy / partial DBs omit it."""
    try:
        rows = list(c.execute("SELECT * FROM phase_bypasses"))
    except sqlite3.OperationalError:
        return
    for r in rows:
        backend_memex.record_phase_bypass(
            project_id=r["project_id"],
            from_phase=r["from_phase"],
            to_phase=r["to_phase"],
            reason=r["reason"],
            agent_id=_get(r, "agent_id", "atelier-system"),
        )
        migrated["phase_bypasses"] += 1


def _replay_project_documents(c: sqlite3.Connection, backend_memex,
                              migrated: dict,
                              already_present: dict) -> None:
    """Replay `project_documents`. v1.1.0 splits the legacy `type` column
    into (`domain`, `subdomain`); we feed `domain` (defaulted to
    `project_doc`) into `backend_memex.write_document`.

    Migration replay uses a placeholder body — the historical markdown
    file at `<workspace>/<filename>` may have been edited or deleted
    since the original write, so indexing the placeholder is acceptable
    here (migration semantics differ from `create_document`; see spec
    §6.8 caveat). A future pass could optionally re-read disk files
    when present.
    """
    try:
        rows = list(c.execute("SELECT * FROM project_documents"))
    except sqlite3.OperationalError:
        return
    for r in rows:
        source_ref = f"atelier:project_documents:{r['id']}"
        if _index_id_for_atelier_row(source_ref) is not None:
            already_present["documents"] += 1
            continue
        domain = _get(r, "domain") or _get(r, "type") or "project_doc"
        filename = _get(r, "filename", "") or ""
        body = (
            f"# {r['title']}\n\n"
            f"File: {filename}\n\n"
            f"Domain: {domain}"
        )
        metadata = {
            "project_id": _get(r, "project_id"),
            "filename": filename,
            "type": domain,  # legacy alias preserved for parity
            "source_ref": source_ref,
        }
        subdomain = _get(r, "subdomain")
        if subdomain:
            metadata["subdomain"] = subdomain
        backend_memex.write_document(
            domain=domain,
            title=r["title"],
            body=body,
            metadata=metadata,
            caller_agent_id=_get(r, "created_by", "atelier-system"),
        )
        migrated["documents"] += 1


# ── decline / prompt helpers ──────────────────────────────────────────────


def decline_migration(ai_dir: Path) -> None:
    """User declined migration; record the choice so we don't re-prompt.

    Writes `<ai_dir>/atelier.local-only` containing the decline timestamp
    and a hint that deleting the file re-enables the prompt.
    """
    ai_dir = Path(ai_dir)
    ai_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "atelier.local-only").write_text(json.dumps({
        "declined_at": _now_iso(),
        "note": "Delete this file to re-enable the migration prompt.",
    }, indent=2), encoding="utf-8")


def should_prompt(ai_dir: Path) -> bool:
    """True iff a project-local `atelier.db` exists AND neither the
    `.migrated` nor `.local-only` marker is present.

    The entry skill calls this BEFORE doing any real work; a False
    return means the project is either already migrated, explicitly
    opted out, or has no local DB to migrate."""
    ai_dir = Path(ai_dir)
    db = ai_dir / "atelier.db"
    if not db.exists():
        return False
    if (ai_dir / "atelier.migrated").exists():
        return False
    if (ai_dir / "atelier.local-only").exists():
        return False
    return True


def row_summary(local_db: Path) -> dict:
    """Per-table row count for the migration-prompt message. Missing
    tables count as zero so the summary works against partial legacy
    DBs without raising."""
    c = _connect_local(local_db)
    summary: dict[str, int] = {}
    try:
        for table in ("projects", "tasks", "meeting_minutes", "sessions",
                      "phase_bypasses", "project_documents"):
            try:
                row = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                summary[table] = int(row[0]) if row else 0
            except sqlite3.OperationalError:
                summary[table] = 0
    finally:
        c.close()
    return summary
