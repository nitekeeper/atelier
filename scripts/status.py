# scripts/status.py
"""Atelier team-mode read-only run-status reporter (issue #65, AC #5).

A PURE-render snapshot of a live (or finished) team-mode cycle. It reads the
SAME durable state the wave scheduler writes — the ``tasks`` table and the
``bridge_messages`` inter-agent log — and renders a human-readable text block
answering three operator questions:

  (1) **Active wave NUMBER** — the lowest-index wave from
      :func:`scripts.pm_dispatch.partition_waves` that is not all-terminal.
      ``partition_waves`` already drops DB-terminal tasks, so its FIRST wave is
      by construction the lowest non-all-terminal wave; we surface that wave's
      ``parallel_group`` as the wave number. ``None`` when every task is
      terminal (the run is done) or there are no tasks.

  (2) **In-flight worker COUNT** — active-wave tasks that are (a) non-terminal,
      (b) have ``attempts > 0`` (i.e. have actually been dispatched at least
      once), and (c) are still within the per-attempt wall-clock cap
      (:data:`scripts.pm_dispatch.WALL_CLOCK_S`), measured ``now -
      last_attempt_at``. A task whose last attempt aged past the cap is NOT
      counted in-flight — the scheduler will soft-kill it; the report mirrors
      that view rather than over-counting silent-dead workers.

  (3) **Latest ENVELOPES** — per role/recipient on the roster
      (``team_members``), the newest VALID terminal reply. We PEEK the bridge
      via :func:`scripts.bridge_read.read_once` with ``update_cursor=False``
      (NEVER advance the delivery cursor from a read-only report), recover each
      worker's JSON body via :func:`scripts.dispatch._parse_reply_envelope`,
      and validate it fail-closed with
      :func:`scripts.pm_dispatch_envelope.validate_envelope`. We render
      ``status`` / ``notes_md`` / ``next_action`` with each artifact preview
      TRUNCATED to :data:`_ARTIFACT_PREVIEW_CAP` chars so a huge envelope can't
      flood the snapshot.

Mode gate: state lives in Local mode (migration-006 mutators are Local-only).
In non-local mode we print a clear "status requires Local mode" notice and
return 0 — read-only, never raising, never mutating.

Untrusted input: every bridge payload is DATA. We parse / validate / TRUNCATE /
echo it inside a clearly-labelled report block — never execute it, never
interpolate it into anything executable. The validator does the same.

All SQL in this module is fully static with ``?``-bound parameters (bandit
B608-clean): no value is ever interpolated into a query string.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from scripts import mode_detector
from scripts.bridge_read import DEFAULT_LIMIT, read_once
from scripts.dispatch import _parse_reply_envelope
from scripts.pm_dispatch import (
    WALL_CLOCK_S,
    partition_waves,
)
from scripts.pm_dispatch_envelope import (
    EnvelopeValidationError,
    validate_envelope,
)

# DB statuses that mean a task needs no (further) dispatch. Mirrors
# pm_dispatch._DB_TERMINAL_STATUSES (the tasks table stores success as
# 'complete' and abandonment as 'abandoned'); we keep our own copy rather than
# importing a private name, but the two MUST agree — a divergence here would
# make the in-flight count disagree with the scheduler's own terminal view.
_DB_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "abandoned"})

#: Per-artifact preview cap (chars) for the rendered envelope. A worker
#: envelope's ``notes_md`` is <= 2k by the SKILL contract, but ``artifacts`` is
#: an unbounded list of arbitrary dicts — truncate each entry's preview so the
#: snapshot stays scannable and a hostile/huge artifact can't flood the report.
_ARTIFACT_PREVIEW_CAP = 200

#: Default project-local DB (atelier uses ONE DB for everything).
_DEFAULT_DB = ".ai/atelier.db"


# ── DB reads (static SQL, ?-bound) ──────────────────────────────────────────


def _open(db_path: str) -> sqlite3.Connection:
    """Open a read-only-intent connection with FK enforcement + Row factory.

    We never write from this module; the connection is used for SELECTs only.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _project_id_for_team(conn: sqlite3.Connection, team_id: str) -> str | None:
    """The ``teams.project_id`` for ``team_id`` (a TEXT id), or ``None`` if the
    team row is absent. Static SQL; ``team_id`` is a bound parameter."""
    row = conn.execute(
        "SELECT project_id FROM teams WHERE team_id = ?",
        (team_id,),
    ).fetchone()
    return None if row is None else row["project_id"]


def _tasks_for_team(
    conn: sqlite3.Connection, team_pk: str, team_id: str
) -> tuple[list[dict[str, Any]], bool]:
    """Load the task rows this team's cycle is scheduling.

    ``team_pk`` is the run/cycle correlation id (NOT FK'd anywhere). Since
    migration 010 the ``tasks`` table carries a ``team_pk`` column, so when a
    project hosts >1 concurrent cycle (``teams.project_id`` has no UNIQUE
    constraint) we can scope the snapshot to THIS cycle's tasks instead of
    conflating the whole project.

    Scoping is COUNT-probe gated so legacy / pre-010 / never-stamped projects
    still render exactly as before (the fallback is MANDATORY, not optional):

      * First scope to the team's PROJECT — the durable join the wave scheduler
        itself uses (``tasks.project_id``). ``teams.project_id`` is TEXT while
        ``tasks.project_id`` is the project rowid, so we compare as TEXT via
        CAST so the bound value matches regardless of which side stored a
        stringified int.
      * Then run a COUNT probe for rows whose ``team_pk`` matches. If >0, add an
        ``AND team_pk=?`` predicate to scope per-cycle. If 0 (all rows NULL /
        a never-stamped legacy project / an unknown team_pk), fall back to the
        project-only WHERE so the snapshot renders exactly as today.

    Returns ``(rows, scoped_by_team_pk)`` so the renderer can surface which
    scoping applied. All SQL is static + ``?``-bound (bandit B608-clean).
    """
    project_id = _project_id_for_team(conn, team_id)
    if project_id is None:
        return [], False
    # COUNT probe: does THIS cycle's team_pk match any task under the project?
    probe = conn.execute(
        "SELECT COUNT(*) FROM tasks "
        "WHERE CAST(project_id AS TEXT) = CAST(? AS TEXT) AND team_pk = ?",
        (project_id, team_pk),
    ).fetchone()
    scoped = bool(probe[0]) if probe is not None else False
    if scoped:
        rows = conn.execute(
            "SELECT * FROM tasks "
            "WHERE CAST(project_id AS TEXT) = CAST(? AS TEXT) AND team_pk = ? "
            "ORDER BY parallel_group, created_at, id",
            (project_id, team_pk),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE CAST(project_id AS TEXT) = CAST(? AS TEXT) "
            "ORDER BY parallel_group, created_at, id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows], scoped


def _recipients_for_team(conn: sqlite3.Connection, team_id: str) -> list[str]:
    """The roster role_ids (``team_members.role_id``) for ``team_id``.

    These are the recipients whose inboxes we peek for reply envelopes. Static
    SQL; ``team_id`` is a bound parameter. Ordered for a stable report.
    """
    rows = conn.execute(
        "SELECT role_id FROM team_members WHERE team_id = ? ORDER BY role_id",
        (team_id,),
    ).fetchall()
    return [r["role_id"] for r in rows]


# ── (1) active wave ─────────────────────────────────────────────────────────


def _is_terminal(task: Mapping[str, Any]) -> bool:
    """True iff ``task``'s DB status means no (further) dispatch is needed.

    Mirrors :data:`_DB_TERMINAL_STATUSES` (``complete`` / ``abandoned``) — the
    same terminal view :func:`partition_waves` itself uses to drop done rows.
    """
    return task.get("status") in _DB_TERMINAL_STATUSES


def _ungrouped_live_tasks(tasks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Non-terminal tasks carrying a NULL ``parallel_group``.

    ``tasks.parallel_group`` is a nullable INTEGER (migration 004 default NULL),
    so a live task created off the planner path can carry NULL.
    :func:`partition_waves` is only safe AFTER ``preflight_validate`` gates NULL,
    which a read-only snapshot deliberately skips — so we segregate these here
    and surface them in a dedicated report line rather than feeding a NULL into
    ``partition_waves``'s sort key (which would raise ``TypeError``).
    """
    return [t for t in tasks if not _is_terminal(t) and t.get("parallel_group") is None]


def _active_wave(tasks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]] | None:
    """The lowest-index non-all-terminal wave, or ``None`` when none remains.

    :func:`partition_waves` excludes DB-terminal tasks, so its first wave is the
    lowest wave that still has live (non-terminal) work. ``None`` means every
    task is terminal (or there are no tasks) — the run is finished.

    Non-terminal tasks with a NULL ``parallel_group`` are EXCLUDED before
    partitioning — ``partition_waves`` sorts by ``parallel_group`` with a bare
    subscript and a NULL co-existing with an int wave raises ``TypeError``. The
    snapshot must "never raise" and be safe mid-cycle, so we drop the ungrouped
    tasks from the wave computation (they are surfaced separately by
    :func:`_ungrouped_live_tasks` in the rendered report).
    """
    groupable = [t for t in tasks if not (not _is_terminal(t) and t.get("parallel_group") is None)]
    waves = partition_waves(groupable)
    return waves[0] if waves else None


def _active_wave_number(active_wave: Sequence[Mapping[str, Any]] | None) -> Any:
    """The ``parallel_group`` value of the active wave, or ``None``.

    Every task in a wave shares one ``parallel_group`` (that is what defines the
    wave), so the first task's value names the wave.
    """
    if not active_wave:
        return None
    return active_wave[0].get("parallel_group")


# ── (2) in-flight count ─────────────────────────────────────────────────────


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (as written by ``backend_local._now`` /
    ``stamp_last_attempt``) into an aware UTC datetime, or ``None`` if absent /
    unparseable. A trailing ``Z`` is normalized to ``+00:00`` for 3.11
    ``fromisoformat``; a naive result is assumed UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_in_flight(task: Mapping[str, Any], *, now: datetime) -> bool:
    """True iff ``task`` counts as an in-flight worker right now.

    Conditions (all required):
      * non-terminal DB status (not complete / abandoned),
      * ``attempts > 0`` (it has actually been dispatched), and
      * within the wall-clock: ``last_attempt_at`` is present AND
        ``now - last_attempt_at < WALL_CLOCK_S``.

    A task past the wall-clock is excluded — the scheduler will soft-kill it, so
    counting it in-flight would over-report silent-dead workers. A task with
    ``attempts > 0`` but no ``last_attempt_at`` stamp is conservatively NOT
    counted (we cannot prove it is within the cap).
    """
    if task.get("status") in _DB_TERMINAL_STATUSES:
        return False
    try:
        attempts = int(task.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts <= 0:
        return False
    last = _parse_ts(task.get("last_attempt_at"))
    if last is None:
        return False
    age_s = (now - last).total_seconds()
    return 0 <= age_s < WALL_CLOCK_S


def _in_flight_count(active_wave: Sequence[Mapping[str, Any]] | None, *, now: datetime) -> int:
    """Count active-wave tasks that are currently in-flight (see
    :func:`_is_in_flight`). Empty/absent active wave → 0."""
    if not active_wave:
        return 0
    return sum(1 for t in active_wave if _is_in_flight(t, now=now))


# ── (3) latest envelopes ────────────────────────────────────────────────────


def _truncate(text: str, cap: int = _ARTIFACT_PREVIEW_CAP) -> str:
    """Cap ``text`` to ``cap`` chars, appending an ellipsis marker when cut.

    ``…(+N more)`` makes the truncation explicit so a reader knows the preview
    is partial (and how much was elided) — never silently swallow content.
    """
    if len(text) <= cap:
        return text
    elided = len(text) - cap
    return f"{text[:cap]}…(+{elided} more)"


def _render_artifact(artifact: Any) -> str:
    """Render one artifact entry as a single truncated preview line.

    The SKILL shape is ``{"path": ..., "sha": ...}`` but artifacts is untrusted
    DATA — entries may be arbitrary. We render ``path`` when present, else the
    ``repr`` of the whole entry, then TRUNCATE. Untrusted: only stringified +
    capped, never executed.
    """
    if isinstance(artifact, Mapping) and artifact.get("path") is not None:
        path = str(artifact.get("path"))
        sha = artifact.get("sha")
        rendered = f"{path}" if sha is None else f"{path} (sha={sha})"
    else:
        rendered = repr(artifact)
    return _truncate(rendered)


def _max_inbox_seq(db_path: str, *, team_id: str, role_id: str) -> int:
    """The MAX ``seq`` in ``role_id``'s inbox for ``team_id``, or 0 if empty /
    unreadable.

    Used to seek the TAIL of a deep inbox: :func:`read_once` returns the OLDEST
    ``limit`` rows (``seq > since_seq ORDER BY seq ASC``), so for an inbox deeper
    than ``DEFAULT_LIMIT`` the genuinely-newest reply would never be read if we
    peeked from ``since_seq=0``. We instead peek from just below the tail. Static
    SQL, ``?``-bound; any read error degrades to 0 (peek from the start)."""
    try:
        conn = _open(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(seq) FROM bridge_messages WHERE team_id=? AND recipient=?",
                (team_id, role_id),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return 0
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _latest_valid_envelope(db_path: str, *, team_id: str, role_id: str) -> dict[str, Any] | None:
    """The newest VALID terminal reply envelope in ``role_id``'s inbox, or
    ``None``.

    PEEK the bridge (``update_cursor=False`` — a read-only report MUST NOT
    advance the delivery cursor; that would hide the row from the real consumer).
    Scan replies newest-first; recover each worker's JSON body via the fence
    parser and validate it fail-closed. Because a read-only report has no
    dispatch record to bind against, we validate each envelope against its OWN
    self-reported ``task_id`` / ``attempt`` (so the check answers "is this a
    well-formed TM-006 envelope?", the right question for display). A read error
    or no valid envelope yields ``None`` — never raises.

    DEEP-INBOX TAIL SEEK: ``read_once`` returns the OLDEST ``DEFAULT_LIMIT`` rows
    (``ORDER BY seq ASC LIMIT``), so peeking from ``since_seq=0`` on an inbox
    deeper than the limit would scan only the oldest window and MISS the newest
    reply. We first find the inbox's max seq and peek from just below the tail
    (``max_seq - DEFAULT_LIMIT``), so the newest reply is always inside the
    returned page regardless of inbox depth.
    """
    max_seq = _max_inbox_seq(db_path, team_id=team_id, role_id=role_id)
    # Seek the tail: start the window so it ends at the newest row. A shallow
    # inbox (max_seq <= DEFAULT_LIMIT) falls back to since_seq=0 (read from the
    # start), preserving the existing behavior for small inboxes.
    since_seq = max(0, max_seq - DEFAULT_LIMIT)
    try:
        rows = read_once(
            db_path,
            team_id=team_id,
            role_id=role_id,
            since_seq=since_seq,
            limit=DEFAULT_LIMIT,
            update_cursor=False,  # PEEK — never advance the delivery cursor
        )
    except Exception:
        # Team not yet created, schema mismatch mid-setup, transient lock, or a
        # non-member role — a read-only report degrades to "no envelope" rather
        # than aborting the whole snapshot.
        return None

    for row in reversed(rows):
        if row.get("kind") != "reply":
            continue
        envelope = _parse_reply_envelope(row.get("payload"))
        if envelope is None:
            continue
        # Bind the validator to the envelope's OWN claimed identity — a display
        # snapshot has no PM dispatch record to cross-check against; this checks
        # well-formedness (type / status / artifacts / abandon grammar).
        env_task_id = envelope.get("task_id")
        env_attempt = envelope.get("attempt")
        if env_task_id is None or env_attempt is None:
            continue
        try:
            validated = validate_envelope(
                envelope,
                dispatched_task_id=env_task_id,
                dispatched_attempt=env_attempt,
            )
        except EnvelopeValidationError:
            continue
        return validated
    return None


def _render_envelope(role_id: str, envelope: dict[str, Any]) -> list[str]:
    """Render a validated envelope for ``role_id`` as report lines.

    Shows ``status`` / ``next_action`` / a TRUNCATED ``notes_md`` / and each
    artifact's TRUNCATED preview. All values are untrusted DATA — stringified,
    capped, and echoed inside the report block only.
    """
    status = envelope.get("status")
    next_action = envelope.get("next_action")
    notes_md = envelope.get("notes_md")
    artifacts = envelope.get("artifacts")

    lines = [
        f"  [{role_id}] status={status} next_action={next_action}",
    ]
    if isinstance(notes_md, str) and notes_md.strip():
        # Collapse to the first line + a truncated preview so a multi-line
        # narrative stays a single scannable report row.
        first_line = notes_md.splitlines()[0]
        lines.append(f"    notes: {_truncate(first_line)}")
    if isinstance(artifacts, list) and artifacts:
        lines.append(f"    artifacts ({len(artifacts)}):")
        for artifact in artifacts:
            lines.append(f"      - {_render_artifact(artifact)}")
    return lines


# ── Public render contract ──────────────────────────────────────────────────


def render_status(db_path: str, *, team_id: str, team_pk: str) -> str:
    """Render the read-only run-status snapshot as text (PURE, testable).

    Reads the ``tasks`` table (scoped to the team's project) and the bridge
    log; renders the active wave number, the in-flight worker count, and the
    latest valid envelope per roster recipient. Never mutates state; never
    advances any delivery cursor; never raises on a missing team / empty roster
    (those degrade to clearly-labelled "none" lines).
    """
    conn = _open(db_path)
    try:
        project_id = _project_id_for_team(conn, team_id)
        tasks, scoped_by_team_pk = _tasks_for_team(conn, team_pk, team_id)
        recipients = _recipients_for_team(conn, team_id)
    finally:
        conn.close()

    active_wave = _active_wave(tasks)
    wave_number = _active_wave_number(active_wave)
    now = datetime.now(timezone.utc)
    in_flight = _in_flight_count(active_wave, now=now)
    ungrouped = _ungrouped_live_tasks(tasks)

    lines: list[str] = [
        "=== atelier run status ===",
        f"team_id: {team_id}",
        f"team_pk: {team_pk}",
        # Surfacing project_id makes the join explicit: tasks scope by
        # project_id (the durable wave-scheduler join). The scope line below
        # then says WHICH cycle's slice rendered.
        f"project_id: {project_id if project_id is not None else '(unresolved)'}",
        # When tasks carry this cycle's team_pk (migration 010), the snapshot
        # scopes the active wave + in-flight count to THIS cycle. Otherwise
        # (legacy / pre-010 / never-stamped project) it falls back to
        # project-wide, which may conflate other cycles sharing the project.
        # See skills/status/SKILL.md "Scope".
        ("scope: cycle (team_pk)" if scoped_by_team_pk else "scope: project (team_pk unpopulated)"),
        "",
    ]

    if not tasks:
        lines.append("active wave: (no tasks for this team)")
    elif wave_number is None:
        if ungrouped:
            # Every grouped task is terminal, but live NULL-group tasks remain —
            # the run is NOT complete, it just has nothing the wave scheduler can
            # order yet. Say so explicitly rather than claim completion.
            lines.append("active wave: (none — no grouped live tasks; see ungrouped below)")
        else:
            lines.append("active wave: (none — all tasks terminal; run complete)")
    else:
        wave_size = len(active_wave) if active_wave else 0
        lines.append(f"active wave: {wave_number} ({wave_size} task(s) in wave)")
    lines.append(f"in-flight workers: {in_flight}")
    if ungrouped:
        # Non-terminal tasks with NULL parallel_group are not yet wave-orderable
        # (the planner gates NULL before dispatch). Surface them so the operator
        # sees them rather than having them silently vanish from the snapshot.
        lines.append(f"ungrouped tasks (NULL parallel_group): {len(ungrouped)}")
    lines.append("")

    lines.append("latest envelopes (per recipient):")
    if not recipients:
        lines.append("  (no team members on the roster)")
    else:
        rendered_any = False
        for role_id in recipients:
            envelope = _latest_valid_envelope(db_path, team_id=team_id, role_id=role_id)
            if envelope is None:
                lines.append(f"  [{role_id}] (no valid terminal envelope yet)")
                continue
            lines.extend(_render_envelope(role_id, envelope))
            rendered_any = True
        if not rendered_any:
            lines.append("  (no valid terminal envelopes reported yet)")

    return "\n".join(lines) + "\n"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="status",
        description="Read-only team-mode run-status reporter (atelier#65 AC#5).",
    )
    p.add_argument(
        "--db",
        default=_DEFAULT_DB,
        help=f"SQLite DB path (default: {_DEFAULT_DB})",
    )
    p.add_argument("--team-id", required=True, dest="team_id", help="team_id to report on")
    p.add_argument(
        "--team-pk",
        required=True,
        dest="team_pk",
        help="run/cycle correlation id (team_pk)",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: print the run-status snapshot; return 0.

    Mode-gated: in non-local mode the dispatch-state columns this report reads
    are not populated (migration-006 mutators are Local-only), so we print a
    clear notice and return 0 rather than render a misleading empty snapshot.
    """
    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    if mode_detector.detect_mode() != "local":
        print(
            "status requires Local mode: team-mode dispatch state "
            "(attempts / last_attempt_at) is Local-mode only "
            "(migration 006). Nothing to report in the current mode.",
        )
        return 0

    print(render_status(args.db, team_id=args.team_id, team_pk=args.team_pk), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
