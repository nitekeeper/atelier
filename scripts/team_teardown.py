# Normal-completion team-mode teardown record — atelier#90 (part 1). The
# happy-path analog of scripts/abort.py: where abort.py is the DELIBERATE
# in-session teardown (operator stops the cycle / the team stalls), this module
# records the NORMAL teardown when a cycle finishes cleanly. Both close the same
# orphan loop scripts/sweep_leaked_teams.py reports against — by enqueuing a
# 'team_delete' bridge row scoped to the team_pk AND setting teams.status='closed'.
#
# Invoked by internal/dev-finish/SKILL.md (agent-team mode only) via
# `PYTHONPATH=. python3 -m scripts.team_teardown`.

"""Record a normal-completion team-mode teardown (atelier#90 part-1).

`record_team_teardown` closes the sweep's over-report window that
`scripts/sweep_leaked_teams.py` documents: a cleanly-completed team would be
re-reported as an orphan until the normal teardown path records it. It:

  1. MODE GATE (mode_detector.detect_mode): non-local mode WARNs + returns 0 —
     team-state mutators are Local-mode-only (mirrors abort.py's non-local skip;
     there is no live team-mode run to tear down outside Local mode).
  2. Enqueues EXACTLY ONE `bridge_requests` row `kind='team_delete'`,
     `status='pending'`, scoped to `team_pk`, `args_json={"team_id": ...,
     "reason": ...}` — the symmetric subtractor that clears
     `sweep_leaked_teams.find_orphan_team_ids`'s orphan-join (filter (i)) once
     dev-finish services it to `status='ready'`. A SELECT-then-INSERT
     idempotency guard returns the EXISTING row's id without a second INSERT
     when one already exists for this (team_pk, team_id), so re-entrant
     dev-finish calls do not double-enqueue.
  3. Sets `teams.status='closed'` for `team_id` — the belt-and-suspenders hedge
     (filter (ii)): the sweep's closed-team CTE matches IMMEDIATELY, even before
     the pending row is flipped to 'ready'. No-op when team_id is None and safe
     when no production teams row exists.
  4. Best-effort `backend.write_team_audit(event_type='completed', ...)` via the
     `scripts.backend` facade (A2), wrapped in the same
     IntegrityError/OperationalError swallow as abort.py (FK to teams; skipped
     when team_id is None).

The `team_delete` enqueue + `teams.status` UPDATE are raw sqlite3 on
`.ai/atelier.db` via this module's own `_connect` (WAL + 5s busy_timeout,
matching abort._connect) — the established always-Local bridge-queue convention
(abort.py, sweep_leaked_teams.py do the same; bridge_requests is always-Local
per scripts/dispatch.py). The ONLY backend call that crosses the facade is the
audit write, routed through scripts/backend.py (A2). All SQL is static + bound
(bandit B608-safe).

CLI: `python3 -m scripts.team_teardown --team-pk PK [--team-id ID]
[--status STATUS] [--reason TEXT] [--db PATH | --bridge-db PATH]`.
`--db` / `--bridge-db` default to `.ai/atelier.db`. `main(argv) -> int`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from scripts import backend, mode_detector


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + a 5s busy timeout.

    Both PRAGMAs are connection-scoped, so they are re-applied on every open
    (mirrors abort._connect + sweep_leaked_teams._connect + backend_local._conn)."""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def _existing_team_delete_id(
    con: sqlite3.Connection, team_pk: str, team_id: str | None
) -> int | None:
    """Return the id of an EXISTING `team_delete` row for this (team_pk,
    team_id), or None if none exists — the idempotency guard's read half.

    `json_extract(args_json, '$.team_id') IS ?` matches the NULL team_id case
    too (`IS NULL` semantics), so a team_id=None teardown is idempotent with
    itself. SQL is fully static + bound (bandit B608-safe)."""
    row = con.execute(
        "SELECT id FROM bridge_requests "
        "WHERE kind = 'team_delete' AND team_pk = ? "
        "AND json_extract(args_json, '$.team_id') IS ? "
        "ORDER BY id DESC LIMIT 1",
        (team_pk, team_id),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _enqueue_team_delete(
    db_path: str | Path, team_pk: str, team_id: str | None, reason: str
) -> int:
    """Enqueue exactly ONE `team_delete` row (status='pending') scoped to
    `team_pk` with `args_json={"team_id": team_id, "reason": reason}`; return
    its rowid. Idempotent via the SELECT-then-INSERT guard: when a row already
    exists for this (team_pk, team_id) its id is returned WITHOUT a second
    INSERT (dev-finish is more re-entrant than abort; double-enqueue would be
    harmless — TeamDelete is idempotent and the sweep dedups via NOT IN — but we
    guard anyway). SQL is fully static + bound (bandit B608-safe)."""
    args_json = json.dumps({"team_id": team_id, "reason": reason})
    con = _connect(db_path)
    try:
        existing = _existing_team_delete_id(con, team_pk, team_id)
        if existing is not None:
            return existing
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
    """UPDATE `teams.status` for `team_id` to `status`. No-op when team_id is
    None (nothing to resolve) and safe when no production teams row exists (the
    UPDATE simply matches zero rows — see the §6.x design note: NO production
    INSERT INTO teams exists outside tests, so this hedge silently no-ops there).
    SQL is fully static + bound (mirrors abort._set_team_status)."""
    if team_id is None:
        return
    con = _connect(db_path)
    try:
        con.execute("UPDATE teams SET status = ? WHERE team_id = ?", (status, team_id))
        con.commit()
    finally:
        con.close()


def record_team_teardown(
    db_path: str | Path,
    team_pk: str,
    team_id: str | None,
    *,
    status: str = "closed",
    reason: str = "normal-completion",
) -> int:
    """Record a normal-completion team teardown; return the enqueued (or
    pre-existing) `team_delete` row id, or 0 in non-local mode.

    See the module docstring for the full contract. In NON-local mode this WARNs
    and returns 0 without any mutation — team-state mutators are Local-only.
    """
    detected = mode_detector.detect_mode()
    if detected != "local":
        print(
            f"team_teardown: detected mode={detected!r}; team-state mutations "
            "(team_delete enqueue / teams.status / team audit) are Local-mode "
            "only and will be SKIPPED (no live team-mode run to tear down "
            "outside Local mode).",
            file=sys.stderr,
        )
        return 0

    # (i) The load-bearing record: exactly one pending team_delete row.
    delete_row_id = _enqueue_team_delete(db_path, team_pk, team_id, reason)

    # (ii) Belt-and-suspenders hedge: set teams.status='closed' so the sweep's
    # closed-team CTE matches IMMEDIATELY, even before the pending row is flipped
    # to 'ready'. No-op when team_id is None / no production teams row exists.
    _set_team_status(db_path, team_id, status)

    # (iii) Best-effort audit ledger event. team_audit_log.team_id REFERENCES
    # teams(team_id), so skip outright when team_id is unresolved; otherwise
    # attempt it but treat an FK miss / transient lock as best-effort (the
    # teardown record already succeeded). Routed through the backend facade (A2).
    if team_id is not None:
        try:
            backend.write_team_audit(
                team_id=team_id,
                event_type="completed",
                payload={
                    "team_pk": team_pk,
                    "reason": reason,
                    "team_delete_request_id": delete_row_id,
                },
            )
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            # IntegrityError: an FK miss (team_id names no teams row, or a row
            # already deleted). OperationalError: a transient lock outlasting the
            # 5s busy_timeout. Either way the audit event is NON-GATING
            # bookkeeping — the team_delete row + teams.status are already
            # written, so a missing audit event must NOT fail the teardown.
            print(
                f"team_teardown: WARN could not write 'completed' audit event for "
                f"team_id={team_id!r} ({type(exc).__name__}: {exc}). "
                "Teardown record is unaffected.",
                file=sys.stderr,
            )

    print(
        f"team_teardown: recorded normal teardown for team_pk={team_pk} "
        f"(team_id={team_id}); teams.status={status if team_id is not None else '(no team_id)'}, "
        f"team_delete row id={delete_row_id} (pending).",
        file=sys.stderr,
    )
    return delete_row_id


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. `main(argv) -> int`.

    Thin wrapper over `record_team_teardown`. The enqueued `team_delete` row is
    PENDING; the LIVE orchestrator session (dev-finish, agent-team mode) must
    service it by calling the harness `TeamDelete` tool and then flipping the
    row to `status='ready'` — exactly like skills/abort/SKILL.md step 4 (the
    bridge-poll servicer does NOT handle the `team_delete` lifecycle kind)."""
    ap = argparse.ArgumentParser(prog="team_teardown")
    # atelier has ONE project-local DB; `--db` is the canonical atelier spelling,
    # `--bridge-db` the kaizen-parity synonym. Both default to '.ai/atelier.db'.
    ap.add_argument("--db", "--bridge-db", default=".ai/atelier.db", dest="db")
    ap.add_argument("--team-pk", required=True, dest="team_pk")
    ap.add_argument(
        "--team-id",
        default=None,
        dest="team_id",
        help="Explicit team_id; dev-finish resolves it from the create_team bridge row.",
    )
    ap.add_argument(
        "--status",
        default="closed",
        help="teams.status to set as the hedge (default 'closed').",
    )
    ap.add_argument(
        "--reason",
        default="normal-completion",
        help="Human-readable teardown reason, recorded in the team_delete row + audit event.",
    )
    args = ap.parse_args(argv)

    record_team_teardown(
        args.db,
        args.team_pk,
        args.team_id,
        status=args.status,
        reason=args.reason,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
