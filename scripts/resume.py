# Aborted-arc resume DETECTOR — atelier#66 [S2] (epic #39 closer, AC3/AC4).
#
# Companion to scripts/abort.py (the abort recorder): where abort.py RECORDS an
# aborted arc (a postmortem doc + an 'aborted' team_audit_log event), resume.py
# DETECTS an aborted-but-incomplete arc on the NEXT /atelier:run pre-flight and
# OFFERS the human a choice (new run vs continue). It is a pure READ — never
# silent, never mutating.
#
# Invoked from scripts/atelier_entrypoint.startup_check() (Local branch only)
# and surfaced by skills/run/SKILL.md's 'Resume detection' section.

"""Never-silent aborted-arc resume DETECTOR for atelier's team lifecycle (#66).

``find_resumable_arc(db_path, *, project_id=None) -> ResumeOffer | None`` is a
static-JSON1 + bootstrap-safe read:

  (0) MODE GATE — ``detect_mode() != 'local'`` returns ``None``. There is no
      Local team-mode dispatch state to resume outside Local mode (mirrors
      abort's non-local skip), so a Memex-mode pre-flight must NOT surface a
      resume offer. §17: team-mode dispatch state is Local-only.

  (1) ``apply_migrations`` first (bootstrap-safe — the read must work against a
      fresh DB; the migration registry's UNIQUE filename gate makes this
      idempotent, the analog of kaizen's bridge_db.bootstrap).

  (2) Detect an aborted-but-incomplete arc: a team whose LATEST *lifecycle*
      audit event is ``'aborted'`` AND which has >= 1 non-terminal task scoped to
      its ``team_pk``.

  (3) Read ``project_id`` + ``abort_phase`` + ``incomplete_task_ids`` from the
      ``'aborted'`` audit payload (``json_extract``) so the SKILL can resume AT
      the right phase without re-planning.

THE DISCRIMINATOR — latest LIFECYCLE event, NOT teams.status
------------------------------------------------------------
``teams.status='closed'`` is reached by a hard abort AND would be by any clean
finish, so detecting 'aborted' via ``teams.status`` alone could FALSE-POSITIVE.
The authoritative signal is the latest *lifecycle* ``team_audit_log`` event:
``abort.py`` writes ``event_type='aborted'``. (The 'completed' lifecycle event
is no longer written — its writer, the team-teardown recorder, was retired in
the host-engine migration — so today every latest-lifecycle row is an 'aborted'
one; the query still keys on the LATEST lifecycle event per team so an aborted
arc's latest is, correctly, 'aborted'.) We compare ONLY the terminal lifecycle
events — ``team_audit_log`` also carries many NON-lifecycle events
(``side_query``, ``roster_consent``, ``persona_gap_escalation``, dispatch/wave
rows), and one of those landing AFTER an abort must NOT mask it. So the query
keys on the latest row whose ``event_type IN ('aborted','completed')``, not the
absolute latest row. ('completed' is kept in the discrimination set so a
historical 'completed' row from before the retirement still correctly suppresses
a resume offer.)

WHY join the project via the audit row, NOT the abort doc
---------------------------------------------------------
The durable abort-report doc is WORKSPACE-LESS (``project_id=None``, #90), so it
cannot identify the project. Resume joins the project via
``team_audit_log.team_id -> teams.project_id`` and reads the textual
``project_id`` correlation string from the 'aborted' payload. The doc is human-
facing offer context only — never the join key.

NEVER SILENT (§3 non-goal / §13)
--------------------------------
``find_resumable_arc`` returns a ``ResumeOffer`` DATA token. It MUST NOT
force-phase, re-dispatch, or mutate any row. The continuation happens ONLY after
the human types 'continue' in the SKILL flow — the detector merely OFFERS.

All SQL is fully static + ?-bound (bandit B608-safe — no f-strings, no
interpolation).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts import mode_detector
from scripts.migrate import apply_migrations

# Migrations live under migrations/shared (001-049) + migrations/local-only
# (050+); apply_migrations is idempotent (the `migrations` UNIQUE filename gate
# skips already-applied files), so running it on read is bootstrap-safe — it
# guarantees teams + tasks + team_audit_log exist before the detection query
# runs.
_MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"

# The TERMINAL LIFECYCLE event_types. abort.py writes 'aborted'. ('completed' is
# no longer written — the team-teardown recorder that wrote it was retired in the
# host-engine migration — but it is retained in this set so a historical
# 'completed' row still discriminates a cleanly-finished arc.) These — and ONLY
# these — discriminate a resumable (aborted) arc; every other team_audit_log
# event_type (side_query, roster_consent, persona_gap_escalation, dispatch/wave
# rows, ...) is NON-lifecycle and must NOT participate in the discrimination, or
# a post-abort non-lifecycle row would mask the abort.
_LIFECYCLE_EVENT_TYPES = ("aborted", "completed")

# Task statuses that mean a task needs no (further) dispatch. Mirrors
# pm_dispatch._DB_TERMINAL_STATUSES (the wave-ordering predicate) so the
# resumable signal ('>= 1 non-terminal task') agrees with partition_waves.
_DB_TERMINAL_STATUSES = ("complete", "abandoned")

# Fully STATIC detection query (bandit B608-safe — no f-strings, no
# interpolation; all values are bound or literal).
#
# `latest_lifecycle` picks, per team, the most-recent row whose event_type is a
# terminal LIFECYCLE event (aborted/completed) — keyed on (created_at, id) so
# same-timestamp rows still order deterministically (id is the AUTOINCREMENT
# tiebreaker). `aborted_team` keeps only teams whose latest lifecycle event is
# 'aborted', pulling project_id + abort_phase from that row's payload. The final
# join confirms >= 1 non-terminal task scoped to the team's team_pk (the team_pk
# itself is recovered from the 'aborted' payload — it is the run/cycle
# correlation id stamped onto tasks by migration 010, NOT teams.project_id).
#
# An explicit `:project_id` filter (optional) narrows to one team's textual
# project_id; passing NULL (the default) matches every aborted team.
_RESUMABLE_SQL = """
WITH lifecycle AS (
  SELECT team_id,
         event_type,
         payload,
         created_at,
         id,
         ROW_NUMBER() OVER (
           PARTITION BY team_id
           ORDER BY created_at DESC, id DESC
         ) AS rn
  FROM team_audit_log
  WHERE event_type IN ('aborted', 'completed')
),
latest_lifecycle AS (
  SELECT team_id, event_type, payload
  FROM lifecycle
  WHERE rn = 1
),
aborted_team AS (
  SELECT ll.team_id,
         json_extract(ll.payload, '$.team_pk')     AS team_pk,
         json_extract(ll.payload, '$.project_id')  AS project_id,
         json_extract(ll.payload, '$.abort_phase') AS abort_phase
  FROM latest_lifecycle ll
  WHERE ll.event_type = 'aborted'
)
SELECT a.team_id,
       a.team_pk,
       a.project_id,
       a.abort_phase,
       (
         SELECT COUNT(*)
         FROM tasks t
         WHERE t.team_pk = a.team_pk
           AND t.status NOT IN ('complete', 'abandoned')
       ) AS incomplete_count
  FROM aborted_team a
 WHERE a.team_pk IS NOT NULL
   AND (:project_id IS NULL OR a.project_id = :project_id)
   AND (
         SELECT COUNT(*)
         FROM tasks t
         WHERE t.team_pk = a.team_pk
           AND t.status NOT IN ('complete', 'abandoned')
       ) >= 1
 ORDER BY a.team_id
 LIMIT 1
"""


@dataclass(frozen=True)
class ResumeOffer:
    """Immutable resume-offer DATA token surfaced to the human by the SKILL.

    Carries everything the 'Resume detection' prose needs to render the verbatim
    new/continue prompt and (on 'continue') force-phase + re-partition the
    PERSISTED tasks WITHOUT re-planning:

      * ``team_id``         — the aborted team's identity (teams.team_id).
      * ``team_pk``         — the run/cycle correlation id (tasks.team_pk).
      * ``project_id``      — the textual project correlation string from the
                              'aborted' payload (teams.project_id), used to
                              reuse the existing project row. May be None if the
                              abort predates the payload extension.
      * ``abort_phase``     — the phase to force-resume AT (workflow.py
                              force-phase). May be None for a legacy abort.
      * ``incomplete_count``— the number of non-terminal tasks remaining.

    The token is frozen because it is OFFER data — the detector must never mutate
    state, and a frozen dataclass makes accidental in-place edits a hard error.
    """

    team_id: str
    team_pk: str
    project_id: str | None
    abort_phase: str | None
    incomplete_count: int


def find_resumable_arc(db_path: str | Path, *, project_id: str | None = None) -> ResumeOffer | None:
    """Detect an aborted-but-incomplete team-mode arc and return a ResumeOffer.

    Returns ``None`` when:
      * the active mode is NOT 'local' (no Local dispatch state to resume — §17);
      * no team's latest lifecycle event is 'aborted' (never aborted, or cleanly
        finished — the latest lifecycle event is 'completed');
      * every task scoped to the aborted team's team_pk is terminal.

    NEVER mutates: this is a pure read. The continuation (force-phase +
    re-partition) is driven by the SKILL only AFTER the human confirms 'continue'.

    ``project_id`` (optional) narrows detection to one team's textual project_id;
    omitted, it matches every aborted team and returns the first by team_id.
    """
    # (0) MODE GATE — outside Local there is no Local team-mode dispatch state to
    #     resume; short-circuit BEFORE touching the DB (mirrors abort's non-local
    #     skip; keeps resume Local-gated and avoids false Memex offers).
    if mode_detector.detect_mode() != "local":
        return None

    # (1) Bootstrap-safe: idempotent migrate-on-read so the detection query works
    #     against a fresh DB (the UNIQUE filename gate skips applied files).
    apply_migrations(str(db_path), _MIGRATIONS_DIR / "shared")
    apply_migrations(str(db_path), _MIGRATIONS_DIR / "local-only")

    con = _connect(db_path)
    try:
        # Pure SELECT — no INSERT/UPDATE/DELETE anywhere in this module. Named
        # `:project_id` bind keeps the SQL static (B608-safe).
        row = con.execute(_RESUMABLE_SQL, {"project_id": project_id}).fetchone()
    finally:
        con.close()

    if row is None:
        return None

    team_id, team_pk, proj_id, abort_phase, incomplete_count = row
    return ResumeOffer(
        team_id=str(team_id),
        team_pk=str(team_pk),
        project_id=None if proj_id is None else str(proj_id),
        abort_phase=None if abort_phase is None else str(abort_phase),
        incomplete_count=int(incomplete_count),
    )


def _connect(db_path: str | Path):
    """Open a SQLite connection with WAL + a 5s busy timeout.

    Both PRAGMAs are connection-scoped, so they are re-applied on every open
    (matches abort._connect). Imported lazily-style at call time to keep the
    module's bare import light, mirroring the sibling scripts' convention."""
    import sqlite3

    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def main(argv: list[str] | None = None) -> int:
    """CLI: print a one-line resume-offer summary, or a no-offer notice.

    ``python3 -m scripts.resume [--db PATH | --bridge-db PATH] [--project-id ID]``.
    A pure read — it NEVER continues an arc (never-silent: the continuation is a
    human decision driven by the SKILL). Always returns 0 (a detection probe must
    never fail the run that calls it)."""
    ap = argparse.ArgumentParser(prog="resume")
    # atelier has ONE project-local DB; `--db` is the canonical spelling,
    # `--bridge-db` the kaizen-parity synonym. Both default to '.ai/atelier.db'.
    ap.add_argument("--db", "--bridge-db", default=".ai/atelier.db", dest="db")
    ap.add_argument(
        "--project-id",
        default=None,
        dest="project_id",
        help="Narrow detection to this team's textual project_id (optional).",
    )
    args = ap.parse_args(argv)

    offer = find_resumable_arc(args.db, project_id=args.project_id)
    if offer is None:
        print("resume: no resumable (aborted-but-incomplete) arc found", file=sys.stderr)
        return 0

    print(
        f"resume: aborted arc found — team_id={offer.team_id} team_pk={offer.team_pk} "
        f"project_id={offer.project_id} abort_phase={offer.abort_phase} "
        f"incomplete_tasks={offer.incomplete_count}. "
        "OFFER ONLY — the human decides new/continue (never silent).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
