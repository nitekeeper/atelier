# Team-mode abort lifecycle — atelier#65 (AC#1/#2). The SOFT (default) +
# --hard teardown writer. Companion to scripts/sweep_leaked_teams.py: where the
# sweep DISCOVERS leaked teams from a prior run, abort.py is the deliberate,
# in-session teardown for the CURRENT team. Both close the same orphan loop by
# enqueuing a 'team_delete' bridge row scoped to the team_pk (migration 009
# widened bridge_requests.kind to admit 'aborted' + 'team_delete').
#
# Invoked by the /atelier:abort SKILL via `PYTHONPATH=. python3 -m scripts.abort`.

"""SOFT + HARD team-mode abort for atelier's team lifecycle (atelier#65).

Two teardown paths over one shared core:

* SOFT (default) — graceful. Sets ``teams.status='shutting_down'`` (an EXISTING
  003 enum value, no migration). Python CANNOT await the live agents, so this
  only INITIATES shutdown: the /atelier:abort SKILL drives the TM-005
  ``shutdown_req`` / ``shutdown_resp`` handshake on the ``bridge_messages`` wire
  in the live session, then services the enqueued ``team_delete`` row via the
  harness ``TeamDelete`` tool. The worktree is PRESERVED when dirty (never
  destroy uncommitted work); a CLEAN worktree is removed only with
  ``--clean-worktree``.
* HARD (``--hard``) — forced. Writes the abort-report doc FIRST (before any
  teardown) so the postmortem survives even if a later step fails, then sets
  ``teams.status='closed'`` and runs the shared core. Auto-cleans the worktree
  ONLY when it is clean; a dirty worktree is PRESERVED + warned (still never
  destroys uncommitted work). The SKILL tells the session to service the
  ``team_delete`` row immediately.

SHARED CORE (BOTH paths run it):
  1. ``backend.write_document(domain='postmortem', subdomain='abort', ...)`` —
     the durable abort-report markdown. This is AC#2, the most-tested invariant:
     BOTH paths MUST produce this doc.
  2. ``backend.write_team_audit(event_type='aborted', ...)`` — the teardown
     ledger event (event_type is free TEXT; no migration).
  3. ONE ``bridge_requests`` row ``kind='team_delete'``, ``status='pending'``,
     scoped to ``team_pk``, ``args_json={"team_id": ...}``. This both INSTRUCTS
     the live session to reap the team via ``TeamDelete`` AND records the
     teardown — the symmetric subtractor that closes
     ``sweep_leaked_teams.find_orphan_team_ids``'s orphan-join.

team_id resolution: an explicit ``--team-id`` wins; otherwise it is resolved
from the ``create_team`` bridge row for ``--team-pk`` via
``json_extract(response_json, '$.team_id')`` (the canonical post-creation
team_id lives in the create_team RESPONSE — see sweep_leaked_teams.py's "WHY
response_json" note).

MODE GATE (mode_detector.detect_mode):
  In ``local`` — do everything (report + audit + enqueue + status mutation +
  worktree handling), and the abort-report doc DOES persist durably (AC#2). In
  NON-local — the migration-006 dispatch-state mutators raise
  ``NotImplementedError``, so we WARN that state mutations are Local-mode only,
  SKIP the DB mutations, WRITE the abort-report, and return 0.

  EXIT CODE: abort ALWAYS exits 0 once a teardown completes — the abort-report
  is a BEST-EFFORT write (it must never fail the teardown that already
  succeeded). A failed report write (a genuine Memex outage → a None backend
  echo) does NOT change the exit code; it is observable ONLY via the stderr
  ERROR line emitted on that path. A nonzero exit therefore signals an
  argparse / dispatch failure before the teardown ran, not a missing report.

  ABORT-REPORT DURABILITY NOW HOLDS IN BOTH MODES (atelier#90 part-3). The
  abort-report is a WORKSPACE-LESS document (``workspace_id=None``,
  migration 005). The Memex facade ``backend.write_document`` now lands a
  workspace-less write under the §6.7 ``_no-workspace_`` key (the
  ``NotImplementedError`` gate is gone), so on the non-local path the report
  PERSISTS just as it does in Local mode — AC#2 holds cross-mode. Only the
  REPORT write crosses modes; team-mode dispatch state (teams.status /
  team_delete enqueue / audit) stays Local-mode-only, so those mutations are
  still SKIPPED in non-local mode (there is no live team-mode run to tear down
  outside Local mode). ``_write_report``'s try/except is now a true last-resort
  guard for a GENUINE Memex outage — a non-persisting report is an ERROR
  condition, not an accepted limitation.

CLI: ``python3 -m scripts.abort --team-pk PK [--team-id ID] [--hard]
[--reason TEXT] [--clean-worktree] [--db PATH | --bridge-db PATH]``.
``--db`` / ``--bridge-db`` default to ``.ai/atelier.db`` (atelier's single
project-local DB). ``main(argv) -> int``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts import backend, mode_detector
from scripts.git_utils import git as _git

# ── The per-session TeamDelete handoff note, embedded verbatim in the report ──
#
# Mirrors sweep_leaked_teams.py's "PER-SESSION TeamDelete LIMITATION" note:
# Python cannot call the session-scoped TeamDelete harness tool, so the live
# orchestrator session SERVICES the enqueued team_delete row, and any
# cross-session config dirs left on disk are filesystem-only cleanup.
_HANDOFF_NOTE = (
    "Per-session TeamDelete handoff: this teardown was recorded by a pure-Python "
    "script, which CANNOT call the session-scoped `TeamDelete` harness tool. A "
    "`team_delete` row (status='pending') was enqueued into this team_pk's "
    "bridge_requests queue carrying the team_id. The LIVE orchestrator session "
    "services it by calling `TeamDelete` during its turn-loop (soft: after the "
    "TM-005 shutdown_req/shutdown_resp handshake completes; hard: immediately). "
    "Any cross-session team config directory left on disk by a crashed prior run "
    "is filesystem-only cleanup: `rm -rf ~/.claude/teams/<team_id>/` (the harness "
    "tool cannot reach another session's directory)."
)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + a 5s busy timeout.

    Both PRAGMAs are connection-scoped, so they are re-applied on every open
    (matches sweep_leaked_teams._connect + scripts/backend_local._conn)."""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def _now_iso() -> str:
    """UTC ISO-8601 timestamp for the report body (display only)."""
    return datetime.now(timezone.utc).isoformat()


def resolve_team_id(db_path: str | Path, team_pk: str) -> str | None:
    """Resolve the team_id for ``team_pk`` from its ``create_team`` bridge row.

    Returns ``json_extract(response_json, '$.team_id')`` of the most-recent
    ``create_team`` row for this team_pk (the canonical post-creation team_id
    lives in the RESPONSE, not the request — see sweep_leaked_teams.py). Returns
    None when no such row exists or the team_id is NULL (e.g. an errored
    create_team that never minted one). SQL is fully static + bound."""
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT json_extract(response_json, '$.team_id') AS team_id "
            "FROM bridge_requests "
            "WHERE kind = 'create_team' AND team_pk = ? "
            "ORDER BY id DESC LIMIT 1",
            (team_pk,),
        ).fetchone()
    finally:
        con.close()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _known_phases(db_path: str | Path) -> set[str]:
    """Return the canonical phase vocabulary — every ``phases.name`` (atelier#66 N1).

    This is the SAME catalog ``scripts/workflow.py`` reads (the static,
    mode-symmetric ``phases`` table seeded by the shared schema migration); we
    read it directly from the always-Local project DB rather than hardcoding a
    list that could drift from the migration. A read failure (e.g. a
    pre-migration DB) returns an EMPTY set — the caller treats "vocabulary
    unknown" as a no-op (records the phase as supplied) so abort never hard-fails
    on a transient catalog read. SQL is fully static (bandit B608-safe)."""
    con = _connect(db_path)
    try:
        rows = con.execute("SELECT name FROM phases").fetchall()
    except sqlite3.Error:
        return set()
    finally:
        con.close()
    return {str(r[0]) for r in rows}


def _validate_phase(db_path: str | Path, abort_phase: str | None) -> str | None:
    """Validate ``--phase`` against the known phase vocabulary (atelier#66 N1).

    RESILIENCE — abort MUST NOT hard-fail on a bad ``--phase`` (the teardown is
    the priority). A typo'd phase that round-trips into ``projects.phase`` on a
    resume-continue would leave a NON-NAVIGABLE phase, so:

      * ``None`` (omitted) → ``None`` (back-compat: a legacy abort carries no
        phase; resume still detects the arc via the audit join).
      * a phase IN the vocabulary → returned as-is.
      * a phase NOT in the vocabulary → a clear WARN to stderr and ``None`` is
        returned, so the BOGUS phase is NEVER propagated into the audit payload /
        abort-report metadata (and thus never into ``projects.phase`` on resume).

    When the vocabulary cannot be read (empty set — pre-migration DB), we do NOT
    second-guess the operator: the phase is recorded as supplied (we cannot prove
    it invalid)."""
    if abort_phase is None:
        return None
    known = _known_phases(db_path)
    if known and abort_phase not in known:
        print(
            f"abort: WARN --phase {abort_phase!r} is not a known phase "
            f"(vocabulary: {sorted(known)}); recording abort_phase=None so a "
            "bogus phase is not propagated into projects.phase on resume. The "
            "abort itself still proceeds.",
            file=sys.stderr,
        )
        return None
    return abort_phase


def _worktree_is_dirty(cwd: Path) -> bool | None:
    """Return True if the worktree has any uncommitted change, False if clean,
    None if the dirty-state could not be determined (not a git repo, git error).

    "Dirty" mirrors worktree.py's convention: a non-empty ``git status
    --porcelain`` (tracked changes OR untracked files). We are deliberately
    CONSERVATIVE — when status cannot be read we return None so callers PRESERVE
    the worktree rather than risk destroying uncommitted work."""
    try:
        result = _git(["status", "--porcelain"], cwd, check=False)
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _remove_worktree(cwd: Path) -> bool:
    """Best-effort `git worktree remove --force` of ``cwd`` from its main
    worktree. Returns True on a clean removal, False otherwise (logged, never
    raised — teardown bookkeeping already succeeded by the time this runs).

    Only ever called once the caller has confirmed the worktree is CLEAN, so
    ``--force`` here removes a clean linked worktree, never uncommitted work."""
    try:
        result = _git(["worktree", "remove", "--force", str(cwd.resolve())], cwd, check=False)
    except (OSError, ValueError) as exc:
        print(f"abort: worktree removal failed ({exc}); preserved.", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            "abort: `git worktree remove` returned "
            f"{result.returncode}; worktree preserved. {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def _handle_worktree(*, mode: str, clean_worktree: bool, cwd: Path) -> str:
    """Apply the path-specific worktree policy; return a human-readable
    description of what happened (folded into the report's "what was torn down").

    NEVER destroys uncommitted work:
      * dirty (or indeterminate) -> ALWAYS preserve (+ warn on hard).
      * clean -> removed iff (hard) OR (soft AND --clean-worktree).
    """
    dirty = _worktree_is_dirty(cwd)
    if dirty is None:
        return "worktree: state indeterminate (not a git worktree / git error) — preserved."
    if dirty:
        if mode == "hard":
            print(
                "abort: worktree is DIRTY — preserving uncommitted work "
                "(hard abort does not destroy uncommitted changes).",
                file=sys.stderr,
            )
        return "worktree: DIRTY — preserved (uncommitted work is never destroyed)."
    # Clean from here on.
    if mode == "hard":
        removed = _remove_worktree(cwd)
        return (
            "worktree: clean — auto-removed (hard abort)."
            if removed
            else "worktree: clean — removal attempted but failed; preserved."
        )
    # Soft path: opt-in only.
    if clean_worktree:
        removed = _remove_worktree(cwd)
        return (
            "worktree: clean — removed (--clean-worktree opt-in)."
            if removed
            else "worktree: clean — removal attempted but failed; preserved."
        )
    return "worktree: clean — preserved (pass --clean-worktree to remove on the soft path)."


def _render_report(
    *,
    team_id: str | None,
    team_pk: str,
    mode: str,
    reason: str,
    timestamp: str,
    torn_down: list[str],
) -> str:
    """Build the abort-report markdown body. Lists team_id / team_pk / mode /
    reason / timestamp, what was torn down, and the TeamDelete handoff note."""
    torn_down_md = "\n".join(f"- {line}" for line in torn_down) if torn_down else "- (none)"
    return (
        f"# Team-mode abort report ({mode})\n\n"
        f"- **team_id:** {team_id if team_id is not None else '(unresolved)'}\n"
        f"- **team_pk:** {team_pk}\n"
        f"- **mode:** {mode}\n"
        f"- **reason:** {reason}\n"
        f"- **timestamp:** {timestamp}\n\n"
        f"## What was torn down\n\n{torn_down_md}\n\n"
        f"## TeamDelete handoff\n\n{_HANDOFF_NOTE}\n"
    )


def _write_report(
    *,
    team_id: str | None,
    team_pk: str,
    mode: str,
    reason: str,
    torn_down: list[str],
    supersedes: int | None = None,
    project_id: str | None = None,
    abort_phase: str | None = None,
    incomplete_task_ids: list[object] | None = None,
) -> dict | None:
    """Write the durable abort-report doc via the mode-dispatched backend.

    domain='postmortem', subdomain='abort', workspace_id=None, project_id=None
    (workspace-less op, migration 005), caller_agent_id='abort'. Returns the
    backend echo dict, or None if the write failed (logged, never raised — in
    HARD mode the report write is intentionally attempted before teardown so a
    failure here does not also lose the teardown bookkeeping).

    ``supersedes`` (when set) is folded into ``metadata`` as the id of the doc
    this one replaces — mirroring ``documents.write_spec_amendment``'s
    ``{version, supersedes}`` chaining. The HARD path writes a crash-survival
    report FIRST then a final ACTUAL-outcomes report that ``supersedes`` it, so
    the two rows are EXPLICITLY linked rather than silent duplicates.

    ``project_id`` / ``abort_phase`` / ``incomplete_task_ids`` (atelier#66) are
    the resume hooks: they are folded into the metadata dict as human-facing
    OFFER context for the next /atelier:run's resume detection. The AUTHORITATIVE
    resume signal is the matching 'aborted' audit payload (the workspace-less
    doc carries project_id=None at the column level — #90 — so resume joins via
    team_audit_log.team_id->teams.project_id, never via this doc). The keys are
    ALWAYS written (defaulting to None) so the metadata schema stays stable for
    resume to read regardless of whether the orchestrator supplied them."""
    body = _render_report(
        team_id=team_id,
        team_pk=team_pk,
        mode=mode,
        reason=reason,
        timestamp=_now_iso(),
        torn_down=torn_down,
    )
    metadata: dict[str, object] = {
        "team_id": team_id,
        "team_pk": team_pk,
        "mode": mode,
        "reason": reason,
        # #66 resume hooks — always present (None when not supplied) so resume's
        # metadata read sees a stable schema.
        "project_id": project_id,
        "abort_phase": abort_phase,
        "incomplete_task_ids": list(incomplete_task_ids) if incomplete_task_ids else [],
    }
    if supersedes is not None:
        metadata["supersedes"] = supersedes
    try:
        return backend.write_document(
            workspace_id=None,
            project_id=None,
            domain="postmortem",
            subdomain="abort",
            title=f"Team abort ({mode}): {team_pk}",
            body=body,
            metadata=metadata,
            caller_agent_id="abort",
        )
    except Exception as exc:  # best-effort durable write; never raise.
        print(f"abort: WARN failed to write abort-report doc: {exc}", file=sys.stderr)
        return None


def _enqueue_team_delete(db_path: str | Path, team_pk: str, team_id: str | None) -> int:
    """INSERT exactly ONE ``team_delete`` row (status='pending') scoped to
    ``team_pk`` with ``args_json={"team_id": team_id}``; return its rowid.

    The live session services this row into a real ``TeamDelete`` call; it is
    also the symmetric subtractor sweep_leaked_teams uses to clear the orphan
    join. SQL is fully static + bound (bandit B608-safe)."""
    args_json = json.dumps({"team_id": team_id})
    con = _connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (team_pk, kind, args_json, status) "
            "VALUES (?, 'team_delete', ?, 'pending')",
            (team_pk, args_json),
        )
        con.commit()
        row_id = cur.lastrowid
        if row_id is None:  # pragma: no cover — AUTOINCREMENT INSERT always yields a rowid
            raise RuntimeError("INSERT returned no lastrowid; team_delete row not enqueued")
        return int(row_id)
    finally:
        con.close()


def _set_team_status(db_path: str | Path, team_id: str | None, status: str) -> None:
    """UPDATE ``teams.status`` for ``team_id`` to ``status`` (an existing 003
    enum value: soft -> 'shutting_down', hard -> 'closed'). No-op when team_id
    is unresolved (nothing to update). SQL is fully static + bound."""
    if team_id is None:
        return
    con = _connect(db_path)
    try:
        con.execute("UPDATE teams SET status = ? WHERE team_id = ?", (status, team_id))
        con.commit()
    finally:
        con.close()


def _do_abort(
    *,
    db_path: str,
    team_pk: str,
    team_id: str | None,
    mode: str,
    reason: str,
    clean_worktree: bool,
    cwd: Path,
    project_id: str | None = None,
    abort_phase: str | None = None,
    incomplete_task_ids: list[object] | None = None,
) -> int:
    """Shared abort core for both paths. Returns a process exit code.

    Order (HARD): report FIRST (persist before teardown), then status mutation,
    then the shared enqueue/audit, then worktree handling. SOFT writes the
    report up front too (cheap, and keeps the two paths symmetric for the AC#2
    "both paths produce the report" invariant) — the only path-specific bits are
    the target teams.status and the worktree policy."""
    detected = mode_detector.detect_mode()

    # Resolve team_id if not supplied. The resolution read is mode-agnostic
    # (always-Local bridge_requests), so it runs in either mode.
    if team_id is None:
        team_id = resolve_team_id(db_path, team_pk)
        if team_id is None:
            print(
                "abort: WARN could not resolve team_id from a create_team row "
                f"for team_pk={team_pk!r}; pass --team-id explicitly if known. "
                "Proceeding — the report still records the abort.",
                file=sys.stderr,
            )

    target_status = "closed" if mode == "hard" else "shutting_down"

    # ── Non-local: attempt the report, skip state mutations ──────────────────
    if detected != "local":
        print(
            f"abort: detected mode={detected!r}; team-state mutations "
            "(teams.status / team_delete enqueue / team audit) are Local-mode "
            "only and will be SKIPPED. Attempting the abort-report only.",
            file=sys.stderr,
        )
        torn_down = [
            f"teams.status -> {target_status}: SKIPPED (non-local mode).",
            "team_delete enqueue: SKIPPED (non-local mode).",
            "team_audit 'aborted' event: SKIPPED (non-local mode).",
            "worktree: untouched (non-local mode).",
        ]
        # The abort-report is workspace-less; the Memex facade now lands a
        # workspace_id=None write under the §6.7 `_no-workspace_` key
        # (atelier#90 part-3), so AC#2 durability holds in non-local mode too
        # (see the module MODE GATE note). A None return signals a GENUINE Memex
        # outage — an ERROR condition that is LOGGED but, because the report is
        # best-effort, does NOT change the exit code: abort still returns 0 once
        # the (skipped, non-local) teardown completes. The ERROR is observable
        # ONLY via the stderr line below — see the module EXIT CODE note.
        report = _write_report(
            team_id=team_id,
            team_pk=team_pk,
            mode=mode,
            reason=reason,
            torn_down=torn_down,
            project_id=project_id,
            abort_phase=abort_phase,
            incomplete_task_ids=incomplete_task_ids,
        )
        if report is None:
            print(
                "abort: ERROR abort-report failed to persist in non-local mode "
                "(workspace-less Memex write should land under the §6.7 "
                "`_no-workspace_` key — a None return indicates a genuine Memex "
                "outage). The teardown completed but the postmortem is missing.",
                file=sys.stderr,
            )
        return 0

    # ── Local mode: full teardown ────────────────────────────────────────────
    torn_down: list[str] = []

    # HARD: write the report FIRST so the postmortem persists even if a later
    # teardown step fails. SOFT writes it here too for path symmetry.
    report_first = mode == "hard"
    report_first_id: int | None = None
    if report_first:
        # The report's "what was torn down" reflects the INTENDED teardown; the
        # subsequent steps below are deterministic local writes.
        planned = [
            f"teams.status -> {target_status}.",
            "bridge_requests: one 'team_delete' row (pending) enqueued.",
            "team_audit_log: one 'aborted' event written.",
        ]
        first_echo = _write_report(
            team_id=team_id,
            team_pk=team_pk,
            mode=mode,
            reason=reason,
            torn_down=planned,
            project_id=project_id,
            abort_phase=abort_phase,
            incomplete_task_ids=incomplete_task_ids,
        )
        # Capture the crash-survival doc's id so the final ACTUAL-outcomes report
        # can supersede-link it (rather than leave two unlinked duplicate docs).
        # Local mode's echo carries the documented `row_id`; `id` (the SELECT *
        # PK) is the fallback for any backend that omits row_id.
        if isinstance(first_echo, dict):
            raw_id = first_echo.get("row_id", first_echo.get("id"))
            try:
                report_first_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                report_first_id = None

    # State mutation: teams.status.
    _set_team_status(db_path, team_id, target_status)
    torn_down.append(f"teams.status -> {target_status}.")

    # Shared-core enqueue: exactly one team_delete row.
    delete_row_id = _enqueue_team_delete(db_path, team_pk, team_id)
    torn_down.append(f"bridge_requests: enqueued 'team_delete' row id={delete_row_id} (pending).")

    # Shared-core audit: one 'aborted' event (team_audit_log is always-Local).
    # team_audit_log.team_id REFERENCES teams(team_id), so the write requires a
    # real teams row. Skip outright when unresolved; otherwise attempt it but
    # treat an IntegrityError (e.g. an explicit --team-id that names no existing
    # team, or a team row already deleted) as best-effort — teardown bookkeeping
    # has already succeeded, so a missing audit event must NOT abort the run.
    if team_id is None:
        torn_down.append(
            "team_audit_log: 'aborted' event SKIPPED (team_id unresolved; FK to teams)."
        )
    else:
        try:
            backend.write_team_audit(
                team_id=team_id,
                event_type="aborted",
                payload={
                    "team_pk": team_pk,
                    "mode": mode,
                    "reason": reason,
                    "team_delete_request_id": delete_row_id,
                    # #66 resume hooks — the AUTHORITATIVE resume signal.
                    # resume.find_resumable_arc joins team_audit_log.team_id ->
                    # teams.project_id and reads abort_phase + team_pk +
                    # incomplete_task_ids from THIS payload (the workspace-less
                    # abort doc carries project_id=None, so it cannot be the join
                    # key — #90). Always present (None / []) for a stable schema.
                    "project_id": project_id,
                    "abort_phase": abort_phase,
                    "incomplete_task_ids": list(incomplete_task_ids) if incomplete_task_ids else [],
                },
            )
            torn_down.append("team_audit_log: wrote 'aborted' event.")
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            # IntegrityError: an FK miss (an explicit --team-id naming no teams
            # row, or a row already deleted). OperationalError: a transient lock
            # that outlasts the 5s busy_timeout. Either way the audit event is
            # NON-GATING bookkeeping — teams.status is already flipped and the
            # team_delete row already enqueued, so a missing audit event must NOT
            # abort the teardown (the steps are independent connections, not a
            # single transaction). Best-effort by design; surface + continue.
            print(
                f"abort: WARN could not write 'aborted' audit event for "
                f"team_id={team_id!r} ({type(exc).__name__}: {exc}). "
                "Teardown bookkeeping is unaffected.",
                file=sys.stderr,
            )
            torn_down.append(
                "team_audit_log: 'aborted' event SKIPPED "
                f"(audit write failed: {type(exc).__name__})."
            )

    # Worktree policy (path-specific; never destroys uncommitted work).
    torn_down.append(_handle_worktree(mode=mode, clean_worktree=clean_worktree, cwd=cwd))

    # SOFT writes the report now (after the work) — symmetric AC#2 invariant.
    # HARD already wrote a report-first crash-survival copy; write a final one
    # whose "what was torn down" reflects ACTUAL outcomes (incl. the worktree
    # decision + enqueued row id). On HARD it carries metadata.supersedes =
    # report_first_id so the two project_documents rows are EXPLICITLY linked
    # (mirroring write_spec_amendment's {version, supersedes}) rather than silent
    # duplicates — backend_local.write_document is a plain INSERT with no upsert,
    # so the chain is by metadata, not row replacement.
    _write_report(
        team_id=team_id,
        team_pk=team_pk,
        mode=mode,
        reason=reason,
        torn_down=torn_down,
        supersedes=report_first_id,
        project_id=project_id,
        abort_phase=abort_phase,
        incomplete_task_ids=incomplete_task_ids,
    )

    print(
        f"abort: {mode} abort complete for team_pk={team_pk} "
        f"(team_id={team_id}); teams.status={target_status}, "
        f"team_delete row id={delete_row_id}.",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. ``main(argv) -> int``.

    Resolves the abort path (soft default / ``--hard``), resolves team_id (or
    accepts ``--team-id``), and dispatches into ``_do_abort``. The worktree
    handled is the current working directory's worktree (the live session runs
    from inside the team's worktree)."""
    ap = argparse.ArgumentParser(prog="abort")
    # atelier has ONE project-local DB; `--db` is the canonical atelier spelling,
    # `--bridge-db` the kaizen-parity synonym. Both default to '.ai/atelier.db'.
    ap.add_argument("--db", "--bridge-db", default=".ai/atelier.db", dest="db")
    ap.add_argument("--team-pk", required=True, dest="team_pk")
    ap.add_argument(
        "--team-id",
        default=None,
        dest="team_id",
        help="Explicit team_id; if omitted, resolved from the create_team bridge row.",
    )
    ap.add_argument(
        "--hard",
        action="store_true",
        help="Forced teardown: teams.status='closed', report-first, auto-clean a CLEAN worktree.",
    )
    ap.add_argument(
        "--reason",
        default="operator-initiated abort",
        help="Human-readable abort reason, recorded in the report + audit event.",
    )
    ap.add_argument(
        "--clean-worktree",
        action="store_true",
        help="SOFT path only: remove the worktree iff it is clean (never destroys dirty work).",
    )
    # #66 resume hooks — optional, threaded from the orchestrator that already
    # holds team_pk. They are folded into the 'aborted' audit payload (the
    # authoritative resume signal) AND the abort-report metadata so the next
    # /atelier:run's resume.find_resumable_arc can resume AT the abort phase
    # without re-planning. Omitted -> the keys default to None, a stable schema.
    ap.add_argument(
        "--project-id",
        default=None,
        dest="project_id",
        help="Textual project_id (teams.project_id) folded into the resume hooks.",
    )
    ap.add_argument(
        "--phase",
        default=None,
        dest="abort_phase",
        help="The phase the arc was aborted AT; resume force-phases here on 'continue'.",
    )
    args = ap.parse_args(argv)

    mode = "hard" if args.hard else "soft"
    # Validate --phase against the canonical phase vocabulary (atelier#66 N1).
    # An unknown phase → WARN + None (never propagate a bogus, non-navigable
    # phase into the audit payload / projects.phase). The abort still proceeds —
    # this is a resilience guard, NOT a hard-fail.
    abort_phase = _validate_phase(args.db, args.abort_phase)
    return _do_abort(
        db_path=args.db,
        team_pk=args.team_pk,
        team_id=args.team_id,
        mode=mode,
        reason=args.reason,
        clean_worktree=args.clean_worktree,
        cwd=Path.cwd(),
        project_id=args.project_id,
        abort_phase=abort_phase,
    )


if __name__ == "__main__":
    sys.exit(main())
