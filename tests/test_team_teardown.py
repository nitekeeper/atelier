"""pytest suite for `scripts/team_teardown.py` — normal happy-path team-mode
teardown record (atelier#90 part-1).

`record_team_teardown` is the NORMAL-completion analog of `scripts/abort.py`'s
deliberate teardown: when a cycle finishes cleanly, dev-finish records the
teardown so `scripts/sweep_leaked_teams.find_orphan_team_ids` no longer
over-reports the cleanly-completed team as an orphan. It enqueues exactly ONE
`team_delete` bridge row (status='pending') scoped to the `team_pk`, sets
`teams.status='closed'` (the forward-safe hedge filter the sweep already
honors), and best-effort writes a `completed` team_audit event via the
`scripts.backend` facade (A2).

These tests stand up a real Local-mode atelier DB (mirroring the `workspace`
fixture in `tests/test_sweep_leaked_teams.py` + `tests/test_abort.py`): chdir
into a fake git root, apply ALL migrations (shared + local-only) against
`.ai/atelier.db`, INSERT a `teams` row + a ready `create_team` bridge row whose
`response_json` carries the `team_id`, and force `detect_mode='local'`.

Coverage:
  * IRON LAW — `find_orphan_team_ids` over-reports the team BEFORE, and is
    cleared AFTER `record_team_teardown` (exactly one `team_delete` row,
    `teams.status='closed'`);
  * idempotency — calling twice yields exactly ONE `team_delete` row and the
    SAME row id (the SELECT-then-INSERT guard), non-vacuous via a neuter;
  * non-local mode gate — `detect_mode='memex'` returns 0, writes NO row, never
    raises (team-state mutators are Local-only);
  * team_id=None resilience — a ready create_team with a NULL team_id still
    enqueues a pending `team_delete` row (args_json team_id=null), the
    `teams.status` UPDATE no-ops, and the audit write is SKIPPED (FK guard)
    without raising.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import backend, mode_detector, team_teardown
from scripts.migrate import apply_migrations
from scripts.sweep_leaked_teams import find_orphan_team_ids

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

TEAM_ID = "team-abc123"
TEAM_PK = "run-2026-05-31-cycle-1"


# ── Local-mode DB fixture (mirrors tests/test_sweep_leaked_teams.py) ─────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace, seeded with
    a `teams` row + a ready `create_team` bridge row carrying the team_id.

    `backend_local._conn()` and `team_teardown`'s `--db .ai/atelier.db` both
    resolve via the CWD git root, so we chdir into the workspace and drop a
    `.git` dir. `detect_mode` is forced to 'local' so the teardown record
    performs the full local write path (state mutation is Local-mode-only)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")
    return {"root": root, "db": str(db)}


def _seed_create_team(db, team_pk: str, team_id: str | None) -> None:
    """INSERT a ready `create_team` bridge row whose `response_json` carries the
    team_id (NULL when team_id is None — an errored create that never minted
    one). Mirrors tests/test_abort.py::workspace + test_sweep_leaked_teams."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO bridge_requests (team_pk, kind, args_json, status, response_json) "
            "VALUES (?, 'create_team', ?, 'ready', ?)",
            (
                team_pk,
                json.dumps({"name": "x", "members": []}),
                json.dumps({"team_id": team_id}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_team_row(db, team_id: str, *, status: str = "active") -> None:
    """INSERT a `teams` row. project_id + lead_role are NOT NULL (003)."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO teams (team_id, project_id, lead_role, status, schema_version) "
            "VALUES (?, 'proj-1', 'atelier-pm-1', ?, 1)",
            (team_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def _team_delete_rows(db, team_pk: str) -> list[dict]:
    """Every kind='team_delete' bridge row for this team_pk (full dicts)."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM bridge_requests WHERE kind = 'team_delete' AND team_pk = ? ORDER BY id",
            (team_pk,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _team_status(db, team_id: str):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT status FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ── IRON LAW — normal teardown clears the sweep over-report window ───────────


def test_normal_teardown_clears_sweep_over_report(workspace):
    """A cleanly-completed team is over-reported as an orphan by the sweep
    until the normal teardown records it. `record_team_teardown`:
      * enqueues EXACTLY ONE pending `team_delete` row scoped to the team_pk,
      * sets teams.status='closed',
    so `find_orphan_team_ids` returns [(team_pk, team_id)] BEFORE and [] AFTER.
    """
    db = workspace["db"]
    _seed_team_row(db, TEAM_ID, status="active")
    _seed_create_team(db, TEAM_PK, TEAM_ID)

    # BEFORE: the cleanly-completed team is over-reported as an orphan.
    assert find_orphan_team_ids(db) == [(TEAM_PK, TEAM_ID)]

    rc = team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)
    assert isinstance(rc, int)

    # AFTER: the over-report window is closed.
    assert find_orphan_team_ids(db) == []

    # Exactly ONE pending team_delete row, args_json carries the team_id.
    del_rows = _team_delete_rows(db, TEAM_PK)
    assert len(del_rows) == 1
    assert del_rows[0]["status"] == "pending"
    assert json.loads(del_rows[0]["args_json"])["team_id"] == TEAM_ID

    # teams.status hedge flipped to 'closed'.
    assert _team_status(db, TEAM_ID) == "closed"


def test_teardown_filter_i_subtracts_without_teams_status_hedge(workspace):
    """Filter (i) — the READY `team_delete` bridge row — subtracts the orphan
    INDEPENDENTLY of the `teams.status='closed'` hedge (filter (ii)).

    `test_normal_teardown_clears_sweep_over_report` seeds a `teams` row and
    `record_team_teardown` flips it to 'closed', so in THAT test the AFTER `[]`
    is achievable via filter (ii) alone (the enqueued `team_delete` stays
    'pending', which filter (i) requires 'ready'). Production has NO
    `INSERT INTO teams`, so filter (ii) is a production no-op and the
    load-bearing subtractor is filter (i) — which fires only once dev-finish
    flips the pending row to 'ready'.

    This variant OMITS the `teams` row entirely (so filter (ii)'s `closed`
    CTE is provably EMPTY) and, after `record_team_teardown`, flips the
    enqueued `team_delete` row to `status='ready'` — then asserts the sweep
    subtracts the team via filter (i) ALONE.
    """
    db = workspace["db"]
    # NO _seed_team_row: filter (ii)'s `teams.status='closed'` CTE is empty,
    # so any subtraction we observe AFTER must come from filter (i).
    _seed_create_team(db, TEAM_PK, TEAM_ID)

    # BEFORE: over-reported as an orphan (no ready team_delete, no closed team).
    assert find_orphan_team_ids(db) == [(TEAM_PK, TEAM_ID)]

    # record_team_teardown enqueues a PENDING team_delete row (filter (i) needs
    # 'ready'); the teams.status hedge no-ops (no teams row) and the audit write
    # FK-fails best-effort (swallowed) — neither contributes to the subtraction.
    team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)

    # Still pending → filter (i) cannot fire yet, and filter (ii) is empty:
    # the orphan MUST still be reported (proves the 'ready' flip is load-bearing).
    assert find_orphan_team_ids(db) == [(TEAM_PK, TEAM_ID)]

    # dev-finish services the row: flip it pending → ready.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE bridge_requests SET status = 'ready' "
            "WHERE kind = 'team_delete' AND team_pk = ?",
            (TEAM_PK,),
        )
        conn.commit()
    finally:
        conn.close()

    # AFTER (ready, still NO teams row): subtracted by filter (i) ALONE.
    assert find_orphan_team_ids(db) == []
    # Sanity: there is genuinely no closed-team hedge in play.
    assert _team_status(db, TEAM_ID) is None


# ── Idempotency (exact-count, non-vacuous) ───────────────────────────────────


def test_record_team_teardown_is_idempotent(workspace):
    """Calling record_team_teardown TWICE with the same (team_pk, team_id)
    yields EXACTLY ONE team_delete row (the SELECT-then-INSERT guard fired) and
    the second call returns the SAME row id."""
    db = workspace["db"]
    _seed_team_row(db, TEAM_ID, status="active")
    _seed_create_team(db, TEAM_PK, TEAM_ID)

    first_id = team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)
    second_id = team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)

    assert first_id == second_id
    assert len(_team_delete_rows(db, TEAM_PK)) == 1


def test_idempotency_guard_is_non_vacuous(workspace, monkeypatch):
    """NON-VACUITY: neuter the SELECT-then-INSERT guard (force the existing-row
    lookup to always miss) and confirm the count assertion goes RED — proving
    the single-row outcome is the guard's doing, not an accident of the fixture.
    """
    db = workspace["db"]
    _seed_team_row(db, TEAM_ID, status="active")
    _seed_create_team(db, TEAM_PK, TEAM_ID)

    # Neuter the guard: pretend no team_delete row ever pre-exists, forcing a
    # second INSERT on the second call.
    monkeypatch.setattr(
        team_teardown, "_existing_team_delete_id", lambda con, team_pk, team_id: None
    )

    team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)
    team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)

    # With the guard neutered, the second call double-enqueues — exact-count RED.
    assert len(_team_delete_rows(db, TEAM_PK)) == 2


# ── Non-local mode gate ──────────────────────────────────────────────────────


def test_non_local_mode_gate_returns_zero_and_writes_nothing(workspace, monkeypatch):
    """In NON-local mode, record_team_teardown WARNs + returns 0, writes NO
    team_delete row, and does NOT raise (team-state mutators are Local-only,
    mirroring abort.py's non-local skip)."""
    db = workspace["db"]
    _seed_team_row(db, TEAM_ID, status="active")
    _seed_create_team(db, TEAM_PK, TEAM_ID)
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    rc = team_teardown.record_team_teardown(db, TEAM_PK, TEAM_ID)

    assert rc == 0
    assert _team_delete_rows(db, TEAM_PK) == []
    # The hedge UPDATE is also skipped — teams.status untouched.
    assert _team_status(db, TEAM_ID) == "active"


# ── team_id=None resilience ──────────────────────────────────────────────────


def test_team_id_none_resilience(workspace, monkeypatch):
    """A ready create_team whose response_json.team_id is NULL still enqueues a
    pending team_delete row (args_json team_id=null), the teams.status UPDATE
    no-ops cleanly, and the audit write is SKIPPED (FK guard) without raising.
    """
    db = workspace["db"]
    _seed_create_team(db, TEAM_PK, None)

    # If team_id=None ever reached the audit write, it would FK-violate; assert
    # the facade audit writer is NEVER called for the None case.
    audit_calls: list = []
    monkeypatch.setattr(
        backend,
        "write_team_audit",
        lambda **kw: audit_calls.append(kw),
    )

    row_id = team_teardown.record_team_teardown(db, TEAM_PK, None)

    assert isinstance(row_id, int)
    del_rows = _team_delete_rows(db, TEAM_PK)
    assert len(del_rows) == 1
    assert del_rows[0]["status"] == "pending"
    # team_id is null in the enqueued args_json.
    assert json.loads(del_rows[0]["args_json"])["team_id"] is None
    # Audit write SKIPPED for the unresolved team_id (FK to teams).
    assert audit_calls == []
