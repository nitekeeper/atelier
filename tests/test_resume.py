"""pytest suite for `scripts/resume.py` — aborted-arc resume DETECTION (atelier#66 [S2]).

`scripts.resume.find_resumable_arc` is the NEVER-SILENT resume DETECTOR: it
discovers an aborted-but-incomplete team-mode arc and returns a ResumeOffer
*data token* the PM surfaces to the human (skills/run/SKILL.md). It is a pure
READ — it MUST NOT force-phase, re-dispatch, or mutate any row. The continuation
only happens AFTER the human types 'continue' (§3 non-goal / §13 never-silent).

The resumable MARKER is built entirely from existing always-Local state (no
migration):

  * team_audit_log LATEST *lifecycle* event_type == 'aborted' (NOT 'completed')
    — the DISCRIMINATOR. teams.status='closed' is reached by BOTH a hard abort
    AND a clean finish (team_teardown), so it false-positives; the latest
    lifecycle audit event is the authoritative signal.
  * >= 1 non-terminal task (tasks.status NOT IN ('complete','abandoned') —
    pm_dispatch._DB_TERMINAL_STATUSES), scoped to the team's team_pk so
    concurrent cycles are not conflated.
  * project_id + abort_phase read from the 'aborted' audit payload (the abort
    doc is workspace-less / project_id=None per #90, so resume joins the project
    via team_audit_log.team_id -> teams.project_id, NOT via the doc).

MODE GATE: find_resumable_arc gates detect_mode()=='local' and returns None in
non-local mode — there is no Local team-mode dispatch state to resume outside
Local mode (mirrors abort/team_teardown's non-local skip). §17.

The Iron-Law test is `test_resume_never_auto_continues`: it asserts the detector
returns an OFFER token and leaves phase/tasks/dispatch untouched. An
implementation that auto-resumes (the §3-violating bug) mutates the phase and
goes RED.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.migrate import apply_migrations

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"
REPO_ROOT = Path(__file__).parent.parent

TEAM_ID = "team-resume-1"
TEAM_PK = "run-2026-05-31-cycle-7"
PROJECT_ID = "proj-7"
ABORT_PHASE = "implement:in-progress"


# ── Local-mode DB fixture (mirrors tests/test_abort.py) ──────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace, seeded with
    a `teams` row + a `projects` row + a couple of `tasks` rows scoped to the
    team's team_pk. Individual tests append the lifecycle audit event(s) they
    need (aborted / completed) via `_audit`.

    `detect_mode` is forced to 'local' so find_resumable_arc performs the full
    detection (it short-circuits to None outside Local mode)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")

    now = "2026-05-31T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    # workspace row: projects.workspace_id REFERENCES workspaces(id) NOT NULL.
    ws_cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("resume-ws", "/tmp/resume-ws", "Resume WS", now, now),
    )
    workspace_id = ws_cur.lastrowid
    # projects row: tasks.project_id REFERENCES projects(id). projects.id is an
    # AUTOINCREMENT INTEGER; capture the rowid so tasks can FK to it. The
    # team's textual project_id (teams.project_id) is a SEPARATE correlation
    # string carried in the audit payload — resume reads THAT from the payload.
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (workspace_id, "resume-proj", "Resume Proj", "design:open", "pm", now, now),
    )
    project_rowid = cur.lastrowid
    # teams row: team_audit_log.team_id REFERENCES teams(team_id). The team's
    # textual project_id is the correlation string folded into the audit payload.
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status, schema_version, created_at) "
        "VALUES (?, ?, ?, 'active', 1, ?)",
        (TEAM_ID, PROJECT_ID, "atelier-pm-1", now),
    )
    # Two tasks scoped to this team_pk: one already complete (terminal), one
    # still pending (non-terminal — the resumable signal).
    conn.execute(
        "INSERT INTO tasks (project_id, title, status, created_by, created_at, updated_at, "
        "parallel_group, team_pk) VALUES (?, 'done-task', 'complete', 'pm', ?, ?, 1, ?)",
        (project_rowid, now, now, TEAM_PK),
    )
    conn.execute(
        "INSERT INTO tasks (project_id, title, status, created_by, created_at, updated_at, "
        "parallel_group, team_pk) VALUES (?, 'live-task', 'pending', 'pm', ?, ?, 1, ?)",
        (project_rowid, now, now, TEAM_PK),
    )
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db), "project_rowid": project_rowid}


# ── Audit / readback helpers ─────────────────────────────────────────────────


def _audit(db_path, team_id, event_type, payload, created_at):
    """Append a team_audit_log row with an explicit created_at so tests can
    order 'aborted' before/after 'completed' deterministically."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO team_audit_log (team_id, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (team_id, event_type, json.dumps(payload), created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _abort_payload():
    return {
        "team_pk": TEAM_PK,
        "mode": "soft",
        "reason": "operator-initiated abort",
        "project_id": PROJECT_ID,
        "abort_phase": ABORT_PHASE,
        "incomplete_task_ids": [2],
    }


def _tasks_snapshot(db_path):
    """All (id, status, attempts) for the workspace tasks, ordered by id — used
    to prove find_resumable_arc does NOT mutate task rows."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, status, attempts FROM tasks ORDER BY id").fetchall()
    finally:
        conn.close()
    return rows


def _project_phase(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT phase FROM projects ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _audit_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM team_audit_log").fetchone()
    finally:
        conn.close()
    return row[0]


def _bridge_message_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM bridge_messages").fetchone()
    finally:
        conn.close()
    return row[0]


def _set_team_status(db_path, team_id, status):
    """Force teams.status — used by the N3 discrimination tests to prove the
    detector keys on the LIFECYCLE event, NOT teams.status."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE teams SET status = ? WHERE team_id = ?", (status, team_id))
        conn.commit()
    finally:
        conn.close()


# ── Iron-Law: never auto-continues ───────────────────────────────────────────


def test_resume_never_auto_continues(workspace):
    """IRON LAW (§3 non-goal / never-silent): find_resumable_arc returns an
    OFFER TOKEN (data) and does NOT force-phase, re-dispatch, or mutate any row.

    The continuation only happens AFTER the human types 'continue' (driven by
    the SKILL prose, not the detector). ANTI-REVERT: an implementation that
    auto-resumes inside find_resumable_arc (the §3-violating bug) would mutate
    the project phase / task rows / audit log, flipping these assertions RED.
    """
    from scripts import resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")

    phase_before = _project_phase(workspace["db"])
    tasks_before = _tasks_snapshot(workspace["db"])
    audit_before = _audit_count(workspace["db"])
    bridge_before = _bridge_message_count(workspace["db"])

    offer = resume.find_resumable_arc(workspace["db"])

    # An OFFER token (data), NOT a side-effecting continuation.
    assert offer is not None
    assert isinstance(offer, resume.ResumeOffer)
    assert offer.team_id == TEAM_ID
    assert offer.abort_phase == ABORT_PHASE
    assert offer.incomplete_count == 1

    # NOTHING was mutated: the detector is a pure read.
    assert _project_phase(workspace["db"]) == phase_before
    assert _tasks_snapshot(workspace["db"]) == tasks_before
    assert _audit_count(workspace["db"]) == audit_before
    assert _bridge_message_count(workspace["db"]) == bridge_before


# ── Discrimination: aborted vs completed ─────────────────────────────────────


def test_offer_when_latest_lifecycle_event_is_aborted(workspace):
    """An OFFER is returned when the LATEST lifecycle event is 'aborted' AND
    there is >= 1 non-terminal task. abort_phase + project_id come from the
    audit payload (NOT from the workspace-less abort doc)."""
    from scripts import resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")

    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None
    assert offer.team_id == TEAM_ID
    assert offer.team_pk == TEAM_PK
    assert offer.project_id == PROJECT_ID
    assert offer.abort_phase == ABORT_PHASE
    assert offer.incomplete_count == 1


def test_no_offer_when_latest_lifecycle_event_is_completed(workspace):
    """DISCRIMINATOR (NOT teams.status): a clean finish writes a 'completed'
    lifecycle event AFTER any earlier 'aborted'. The latest lifecycle event is
    'completed' → NO offer, even though non-terminal tasks may remain. This is
    the false-positive teams.status='closed' would have produced."""
    from scripts import resume

    # An earlier aborted, then a LATER completed (clean re-run finish).
    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")
    _audit(
        workspace["db"],
        TEAM_ID,
        "completed",
        {"team_pk": TEAM_PK, "reason": "normal teardown"},
        "2026-05-31T02:00:00Z",
    )

    assert resume.find_resumable_arc(workspace["db"]) is None


def test_non_lifecycle_events_after_abort_do_not_mask_it(workspace):
    """A non-lifecycle audit event (e.g. 'side_query') written AFTER the abort
    must NOT mask it: only 'aborted'/'completed' are lifecycle-terminal. The
    detector keys on the latest LIFECYCLE event, not the absolute latest row."""
    from scripts import resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")
    # A later non-lifecycle event must be ignored by the discriminator.
    _audit(
        workspace["db"],
        TEAM_ID,
        "side_query",
        {"team_pk": TEAM_PK},
        "2026-05-31T03:00:00Z",
    )

    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None
    assert offer.abort_phase == ABORT_PHASE


def test_no_offer_when_all_tasks_terminal(workspace):
    """No resumable arc when every task scoped to the team_pk is terminal —
    there is nothing left to dispatch even though the latest event is 'aborted'."""
    from scripts import resume

    # Flip the lone non-terminal task to terminal.
    conn = sqlite3.connect(workspace["db"])
    conn.execute("UPDATE tasks SET status = 'abandoned' WHERE status = 'pending'")
    conn.commit()
    conn.close()

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")

    assert resume.find_resumable_arc(workspace["db"]) is None


def test_no_offer_when_no_aborted_event(workspace):
    """No lifecycle event at all → None (an arc that was never aborted)."""
    from scripts import resume

    assert resume.find_resumable_arc(workspace["db"]) is None


# ── #66 N3: teams.status is IRRELEVANT — the discriminator is event-based ─────


def test_closed_status_with_completed_event_returns_none(workspace):
    """#66 N3 (a): teams.status='closed' (the CLEAN-finish state) + latest
    lifecycle event='completed' → NO offer. teams.status='closed' is reached by
    BOTH a hard abort AND a clean finish, so a future teams.status='closed'
    SHORTCUT filter would FALSE-POSITIVE here. This pins that the discriminator
    is the latest LIFECYCLE EVENT, and teams.status is irrelevant.

    ANTI-REVERT: if someone adds a `teams.status` filter to find_resumable_arc,
    this case (closed + completed, with a non-terminal task still present) would
    either flip to a spurious offer or be filtered for the wrong reason."""
    from scripts import resume

    # Clean-finish state: status closed AND the latest lifecycle event completed.
    _set_team_status(workspace["db"], TEAM_ID, "closed")
    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")
    _audit(
        workspace["db"],
        TEAM_ID,
        "completed",
        {"team_pk": TEAM_PK, "reason": "clean finish"},
        "2026-05-31T02:00:00Z",
    )
    # The workspace fixture still has a non-terminal 'pending' task, so a
    # teams.status-only or task-only filter could mis-fire — only the
    # event-based discriminator correctly returns None here.
    assert resume.find_resumable_arc(workspace["db"]) is None


def test_closed_status_with_aborted_event_returns_offer(workspace):
    """#66 N3 (b): teams.status='closed' (e.g. after a HARD abort, which also
    sets 'closed') + latest lifecycle event='aborted' + >= 1 non-terminal task →
    an offer IS returned. Mirror of (a): same teams.status, opposite lifecycle
    event → opposite outcome, proving teams.status does NOT participate in the
    decision."""
    from scripts import resume

    _set_team_status(workspace["db"], TEAM_ID, "closed")
    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")

    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None
    assert offer.team_id == TEAM_ID
    assert offer.abort_phase == ABORT_PHASE
    assert offer.incomplete_count == 1


# ── Mode gate: short-circuit to None outside Local ───────────────────────────


def test_non_local_mode_short_circuits_to_none(workspace, monkeypatch):
    """MODE GATE (§17): in non-local mode there is no Local team dispatch state
    to resume, so find_resumable_arc returns None BEFORE touching the DB. We
    seed a real aborted arc to prove the gate (not the absence of data) is the
    reason for None."""
    from scripts import resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")
    # Local mode would offer; flip to memex and the gate must short-circuit.
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    assert resume.find_resumable_arc(workspace["db"]) is None


# ── Preservation + no re-plan ────────────────────────────────────────────────


def test_tasks_envelopes_audit_preserved_and_no_replan(workspace):
    """AC4: the detect + the resume re-entry path PRESERVE tasks/envelopes/audit
    and do NOT re-plan. find_resumable_arc mutates nothing; partition_waves over
    the persisted tasks dispatches ONLY non-terminal tasks (no planner call)."""
    from scripts import pm_dispatch, resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")

    tasks_before = _tasks_snapshot(workspace["db"])
    audit_before = _audit_count(workspace["db"])

    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None

    # Detector mutated nothing.
    assert _tasks_snapshot(workspace["db"]) == tasks_before
    assert _audit_count(workspace["db"]) == audit_before

    # The resume re-entry reuses the PERSISTED tasks via partition_waves, which
    # drops only terminal rows — NO re-plan. Read the persisted task rows and
    # partition them: only the non-terminal 'pending' task is dispatched.
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, status, parallel_group, created_at FROM tasks WHERE team_pk = ? ORDER BY id",
            (TEAM_PK,),
        ).fetchall()
    ]
    conn.close()

    waves = pm_dispatch.partition_waves(rows)
    dispatched_ids = [t["id"] for wave in waves for t in wave]
    # Only the non-terminal task is dispatched; the complete one is dropped.
    assert dispatched_ids == [2]


# ── AC4 continuation: the SKILL-documented force-phase recipe is EXECUTABLE ───
#
# These tests pin skills/run/SKILL.md's 'On continue' recipe against the REAL
# `scripts/workflow.py` CLI so prose/CLI drift is caught (the #66 review found
# the documented command was non-executable two ways: it omitted the integer
# project_id positional, AND it implied the TEXTUAL teams.project_id — surfaced
# on ResumeOffer.project_id — could be passed where force-phase needs the integer
# projects.id PK). The mode gate is satisfied by pointing HOME at a Memex-less
# tmp dir so the workflow.py subprocess resolves Local mode (write hits the same
# `<git-root>/.ai/atelier.db` the fixture created).


def _workflow(args, *, cwd, home):
    """Drive `scripts/workflow.py` as a subprocess from `cwd` with HOME=`home`
    (Memex-less → Local mode), returning the CompletedProcess."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "workflow.py"), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        encoding="utf-8",
    )


@pytest.fixture
def memexless_home(tmp_path):
    """A HOME with no `~/.memex` install so a child `workflow.py` resolves Local
    mode (the force-phase write must land in the project-local `.ai/atelier.db`,
    not route through a Memex backend)."""
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_documented_force_phase_command_is_executable(workspace, memexless_home):
    """GREEN-after: the CORRECTED 'On continue' command —
    `workflow.py <db> force-phase <integer projects.id> <abort_phase>` — runs
    end-to-end and actually writes the phase. The integer projects.id is the
    fixture's `project_rowid` (what `scope.resolve_scope().project['id']` yields
    in production); abort_phase is `offer.abort_phase` read from the audit payload.
    """
    from scripts import resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")
    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None
    assert offer.abort_phase == ABORT_PHASE

    project_rowid = workspace["project_rowid"]
    assert _project_phase(workspace["db"]) == "design:open"  # pre-resume phase

    proc = _workflow(
        [workspace["db"], "force-phase", str(project_rowid), offer.abort_phase],
        cwd=workspace["root"],
        home=memexless_home,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"Phase forced to: {ABORT_PHASE}" in proc.stdout
    # The continuation actually re-entered the phase the arc was aborted AT.
    assert _project_phase(workspace["db"]) == ABORT_PHASE


def test_old_force_phase_recipe_without_project_id_crashes(workspace, memexless_home):
    """RED-before (anti-revert): the ORIGINAL documented command —
    `force-phase <abort_phase>` with no integer project_id positional — is
    NON-executable. workflow.py treats argv after the known `force-phase` command
    as the project_id and `int('implement:in-progress')` raises ValueError. If a
    future edit reverts the recipe to the no-project-id form, this guard catches
    it (the documented command must never crash as written)."""
    proc = _workflow(
        [workspace["db"], "force-phase", ABORT_PHASE],
        cwd=workspace["root"],
        home=memexless_home,
    )
    assert proc.returncode != 0
    assert "ValueError" in proc.stderr
    assert "invalid literal for int()" in proc.stderr


def test_textual_project_id_cannot_be_force_phased(workspace, memexless_home):
    """RED-before (identity mismatch): ResumeOffer.project_id carries the TEXTUAL
    teams.project_id ('proj-7'), NOT the integer projects.id. Passing it straight
    into force-phase's project_id positional crashes — proving the SKILL MUST
    resolve the integer projects.id (via scope.resolve_scope().project['id'])
    BEFORE force-phase, never plug offer.project_id in directly."""
    from scripts import resume

    _audit(workspace["db"], TEAM_ID, "aborted", _abort_payload(), "2026-05-31T01:00:00Z")
    offer = resume.find_resumable_arc(workspace["db"])
    assert offer is not None
    assert offer.project_id == PROJECT_ID  # textual correlation string, not an int

    proc = _workflow(
        [workspace["db"], "force-phase", offer.project_id, offer.abort_phase],
        cwd=workspace["root"],
        home=memexless_home,
    )
    assert proc.returncode != 0
    assert "ValueError" in proc.stderr
    assert "invalid literal for int()" in proc.stderr
