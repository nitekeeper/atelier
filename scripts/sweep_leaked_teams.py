# Orphan-team cleanup utility — atelier#65 team-mode lifecycle (Layer 3 of
# "leaked-team recovery"). Ports kaizen's canonical
# scripts/sweep_leaked_teams.py to atelier's single project-local DB
# (.ai/atelier.db) and its bridge_requests kind vocabulary. Invoked from
# the /atelier:abort + /atelier:status lifecycle skills and from a next-run
# sweep to catch any team that was created but never torn down.

"""JSON1 orphan-team finder + `aborted` enqueuer for atelier's next-run sweep.

Mirrors kaizen's canonical bridge-DB SQL-join contract (atelier#65 AC#4:
"enqueues an 'aborted' row into the bridge DB scoped to the current run"),
adapted to atelier's schema: ONE project-local SQLite DB (``.ai/atelier.db``)
holding teams + bridge_requests, with the kind vocabulary
``create_team`` / ``team_delete`` (migration 008 + the 009 enum widen).

An ORPHAN team is a ``bridge_requests`` row with ``kind='create_team'`` AND
``status='ready'`` AND ``created_at >= datetime('now','-7 days')`` whose
``response_json '$.team_id'`` is NON-NULL and is NEITHER

  (i)  in the set of ``team_delete`` rows' ``args_json '$.team_id'``
       (``kind='team_delete'``, ``status='ready'``)  — the teardown marker
       abort.py enqueues, the symmetric subtractor of the orphan-join;
  (ii) in the set of teams whose ``teams.status='closed'``  — a forward-safe
       hedge so any future / normal happy-path teardown that sets
       ``teams.status`` is also respected even before it grows a
       ``team_delete`` enqueue.

``find_orphan_team_ids`` returns ``(team_pk, team_id)`` pairs: ``team_pk``
from the ``create_team`` row (atelier's run/cycle correlation id, NOT FK'd to
``teams``), ``team_id`` from that row's ``response_json``.

WHY response_json, NOT args_json
--------------------------------
The canonical team_id post-creation lives in the create_team RESPONSE, not
the request. A ``create_team`` row's ``args_json`` carries only the requested
``name`` + ``members``; the harness mints the real team_id and the
orchestrator writes it back into ``response_json -> {"team_id": "..."}`` when
it flips the row to ``status='ready'``. The ``status='ready'`` filter encodes
the contract that the harness call already SUCCEEDED, so a ready create_team
row always has a valid ``response_json.team_id`` (an errored create_team is
``status='error'`` and never has one). Re-audit this CTE if that contract
changes (e.g. if a future writer marks create_team rows 'error' AFTER a
successful TeamCreate for partial-failure reporting).

PER-SESSION TeamDelete LIMITATION — what this script CAN and CANNOT do
---------------------------------------------------------------------
This script is pure Python and **CANNOT call the ``TeamDelete`` harness
tool**. ``TeamDelete`` is session-scoped (it takes no parameters and operates
only on the team owned by the *current* orchestrator session's turn-loop), so
there is no way to invoke it from a detached process or against an arbitrary
team_id. This script therefore only **ENQUEUES** a single ``aborted`` row
carrying the at-risk team_ids; it never deletes anything itself.

The live orchestrator session SERVICES that ``aborted`` row: it reads
``args_json['team_ids_at_risk']`` and calls ``TeamDelete`` once per id during
its turn-loop. That removes only the in-session team handle / config the
current session can reach. CROSS-SESSION config directories left on disk by a
crashed prior run are FILESYSTEM-ONLY cleanup — the operator removes them with
``rm -rf ~/.claude/teams/<team_id>/`` (the harness tool cannot reach another
session's dir).

OVER-REPORTING IS SAFE (window now closed by the normal teardown path).
atelier#90 part-1 landed the NORMAL happy-path teardown record:
``scripts/team_teardown.record_team_teardown`` (driven by
internal/dev-finish/SKILL.md in agent-team mode) now enqueues a ``team_delete``
row AND sets ``teams.status='closed'`` on clean completion — so a
cleanly-completed team is subtracted by BOTH filter (i) (once dev-finish
services the pending row to ``status='ready'``) AND filter (ii) (immediately, via
the ``teams.status='closed'`` hedge). Any residual over-report (e.g. a crash
between completion and the dev-finish teardown call) remains SAFE: the enqueued
``TeamDelete`` is idempotent — deleting an already-gone team is a no-op. Filter
(ii) above is precisely the hedge that closes the window the instant the normal
teardown sets the status, even before its pending ``team_delete`` row is flipped.

MODE — best-effort, never throws
--------------------------------
``main`` is a best-effort sweep and ALWAYS returns 0 (a failing sweep must
never fail the run that calls it). The orphan-finder is a pure read; in
non-local mode the enqueue is still a plain row INSERT into the same local
``.ai/atelier.db`` (the bridge_requests queue is always-Local, per
scripts/dispatch.py), so no mode gate is needed here — the read/report path
works regardless.

CLI: ``python3 -m scripts.sweep_leaked_teams [--bridge-db PATH | --db PATH]
[--team-pk TEAM_PK | --enqueue-into-run TEAM_PK]``. Without a team_pk, prints
the orphan list to stdout (one ``team_pk\tteam_id`` per line). With one,
INSERTs a single ``aborted`` row scoped to that team_pk with
``team_ids_at_risk`` populated. ``--team-pk`` is the canonical flag;
``--enqueue-into-run`` is preserved as a backward-compatible alias mapping to
the SAME destination for any pre-existing callers.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from scripts.migrate import apply_migrations

# Migrations live under migrations/shared (001-049) + migrations/local-only
# (050+); scripts/migrate.apply_migrations is idempotent (a `migrations`
# UNIQUE filename gate skips already-applied files), so calling it on read is
# the bootstrap-safe analog of kaizen's bridge_db.bootstrap — it guarantees
# bridge_requests + teams exist before the orphan query runs.
_MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"

# Fully STATIC orphan query (bandit B608-safe — no f-strings, no interpolation;
# all values are bound or literal). json_extract pulls the canonical team_id
# from the create_team RESPONSE; the two NOT IN subqueries are the (i)
# team_delete-marker and (ii) closed-team hedge exclusions.
_ORPHAN_SQL = """
WITH created AS (
  SELECT team_pk,
         id AS req_id,
         json_extract(args_json, '$.name') AS name,
         json_extract(response_json, '$.team_id') AS team_id,
         created_at
  FROM bridge_requests
  WHERE kind = 'create_team' AND status = 'ready'
    -- Scope the sweep to recent runs only. Older orphan team_ids may have
    -- been reaped by the harness's own TTL already; re-enqueuing TeamDelete
    -- on them produces futile aborted rows. 7 days is comfortably longer
    -- than any plausible atelier run (matches kaizen's window).
    --
    -- FORMAT-EXACT COMPARISON: created_at is stored in the 008/009 schema
    -- default format strftime('%Y-%m-%dT%H:%M:%fZ','now') => the 'T'-separated,
    -- trailing-'Z' ISO form. The window threshold MUST use the SAME strftime
    -- format (NOT datetime('now','-7 days'), which yields the space-separated,
    -- no-'Z' form): this is a LEXICAL TEXT comparison, and a 'T' (0x54) vs space
    -- (0x20) mismatch at index 10 flips the order at the boundary day. Matching
    -- both sides to '%Y-%m-%dT%H:%M:%fZ' makes the 7-day cutoff exact.
    AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days')
),
deleted AS (
  -- (i) the symmetric subtractor: a ready team_delete marker for this id.
  SELECT json_extract(args_json, '$.team_id') AS team_id
  FROM bridge_requests
  WHERE kind = 'team_delete' AND status = 'ready'
),
closed AS (
  -- (ii) forward-safe hedge: any team whose lifecycle reached 'closed' is
  -- already torn down even if no team_delete row was enqueued.
  SELECT team_id FROM teams WHERE status = 'closed'
)
SELECT c.team_pk, c.team_id
  FROM created c
 WHERE c.team_id IS NOT NULL
   AND c.team_id NOT IN (
     SELECT team_id FROM deleted WHERE team_id IS NOT NULL
   )
   AND c.team_id NOT IN (
     SELECT team_id FROM closed WHERE team_id IS NOT NULL
   )
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + a 5s busy timeout.

    Both PRAGMAs are connection-scoped in SQLite, so they are re-applied on
    every open (matches the connection convention in scripts/backend_local.py
    and scripts/migrate.py).
    """
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def find_orphan_team_ids(db_path: str | Path) -> list[tuple[str, str]]:
    """Return ``(team_pk, team_id)`` tuples for orphan teams.

    ``team_pk`` is the run/cycle correlation id of the ``create_team`` row that
    leaked the team — useful when the operator wants to know WHICH past
    run/cycle left it dangling. Runs migrations first (bootstrap-safe) so the
    read works against a fresh DB.
    """
    apply_migrations(str(db_path), _MIGRATIONS_DIR / "shared")
    apply_migrations(str(db_path), _MIGRATIONS_DIR / "local-only")
    con = _connect(db_path)
    try:
        cur = con.execute(_ORPHAN_SQL)
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    finally:
        con.close()


def enqueue_aborted_row(
    db_path: str | Path,
    team_pk: str,
    orphan_team_ids: list[str],
    reason: str = "next-run sweep: orphan create_team(s) from prior run(s)",
) -> int:
    """INSERT a single ``aborted`` row into ``team_pk``'s queue; return lastrowid.

    The ``aborted`` row carries ``{"reason": ..., "team_ids_at_risk": [...]}``
    in ``args_json``. The live orchestrator session services it by calling
    ``TeamDelete`` on each id in ``team_ids_at_risk`` — Python is the producer;
    the orchestrator does NOT re-derive the list via SQL.
    """
    args_json = json.dumps({"reason": reason, "team_ids_at_risk": list(orphan_team_ids)})
    con = _connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (team_pk, kind, args_json, status) "
            "VALUES (?, 'aborted', ?, 'pending')",
            (team_pk, args_json),
        )
        con.commit()
        row_id = cur.lastrowid
        if row_id is None:  # pragma: no cover — AUTOINCREMENT INSERT always yields a rowid
            raise RuntimeError("INSERT returned no lastrowid; aborted row not enqueued")
        return int(row_id)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    """Best-effort sweep entry point — ALWAYS returns 0.

    Without ``--team-pk`` (or its ``--enqueue-into-run`` alias): print the
    orphan list to stdout. With one: enqueue exactly ONE ``aborted`` row scoped
    to that team_pk carrying the orphan team_ids. Either way, return 0 — a
    sweep must never fail the run that calls it.
    """
    ap = argparse.ArgumentParser(prog="sweep_leaked_teams")
    # `--bridge-db` is the canonical flag name (parity with kaizen); `--db` is
    # accepted as a synonym because atelier has ONE project-local DB and other
    # atelier CLI scripts spell the flag `--db`. Both default to '.ai/atelier.db'.
    ap.add_argument("--bridge-db", "--db", default=".ai/atelier.db", dest="bridge_db")
    # `--team-pk` is the canonical flag; `--enqueue-into-run` is preserved as a
    # backward-compatible ALIAS mapping to the SAME destination so any
    # pre-existing caller that passes the old name keeps working unchanged.
    ap.add_argument(
        "--team-pk",
        "--enqueue-into-run",
        default=None,
        dest="team_pk",
        help="If set, enqueue one 'aborted' row in this team_pk's queue carrying the orphan team_ids.",
    )
    args = ap.parse_args(argv)

    orphans = find_orphan_team_ids(args.bridge_db)
    if not orphans:
        print("sweep_leaked_teams: no orphan team_ids found", file=sys.stderr)
        return 0

    if args.team_pk is None:
        for origin_team_pk, team_id in orphans:
            print(f"{origin_team_pk}\t{team_id}")
        return 0

    team_ids = [tid for _, tid in orphans]
    row_id = enqueue_aborted_row(args.bridge_db, args.team_pk, team_ids)
    print(
        f"sweep_leaked_teams: enqueued aborted row id={row_id} into team_pk "
        f"{args.team_pk} with {len(team_ids)} team_ids_at_risk",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
