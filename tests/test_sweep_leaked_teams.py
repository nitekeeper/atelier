"""Tests for scripts/sweep_leaked_teams.py — atelier#65 JSON1 orphan finder.

Atelier ports kaizen's canonical sweep to its SINGLE project-local DB
(`.ai/atelier.db`, holding both `teams` and `bridge_requests`) and its
`create_team` / `team_delete` kind vocabulary (migration 008 + the 009 enum
widen). These tests stand up a real Local-mode atelier DB by applying ALL
migrations (`scripts.migrate.apply_migrations` over both `migrations/shared`
and `migrations/local-only`), then INSERT `bridge_requests` / `teams` rows
directly via sqlite3 — mirroring the Local-mode `workspace` fixture pattern
from `tests/test_pm_dispatch.py`.

Coverage (non-vacuous, exact-count where a count matters):

* orphan detected when a `create_team` has NO matching `team_delete` and the
  team is not closed → `main(--team-pk ...)` enqueues EXACTLY ONE `aborted`
  row whose `args_json.team_ids_at_risk` == the orphan ids;
* matched create + `team_delete` → NOT an orphan (guards the team_delete NOT IN
  exclusion — fails if that exclusion is reverted);
* `teams.status='closed'` suppresses the orphan (guards the closed-team NOT IN
  hedge — fails if that exclusion is reverted);
* the 7-day window excludes an old `create_team`;
* an errored (`status != 'ready'`) `create_team` is NOT treated as an orphan;
* `main()` returns 0 on the no-orphan, print, and enqueue paths.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.migrate import apply_migrations
from scripts.sweep_leaked_teams import (
    enqueue_aborted_row,
    find_orphan_team_ids,
    main,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ── Local-mode DB fixture (mirrors tests/test_pm_dispatch.py) ────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace.

    `detect_mode` is forced to 'local' (matches CI) and we chdir into a fake
    git root, exactly like `tests/test_pm_dispatch.py::workspace`. The sweep is
    pure SQL against the DB path we pass explicitly, so it does not depend on
    CWD resolution — but we keep the Local-mode fixture shape for parity and so
    the autouse mode-cache / singleton-workspace conftest fixtures behave."""
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


# ── Direct-INSERT seeders (bridge_requests + teams) ─────────────────────────


def _seed_create_team(
    db,
    team_pk: str,
    name: str,
    team_id: str,
    *,
    status: str = "ready",
    created_at: str | None = None,
) -> int:
    """INSERT a `create_team` bridge_requests row.

    The canonical team_id post-creation lives in `response_json.team_id` (the
    harness mints it on a successful TeamCreate). `team_pk` is a TEXT
    correlation id (008: `team_pk TEXT NOT NULL`)."""
    con = sqlite3.connect(db)
    try:
        if created_at is None:
            cur = con.execute(
                "INSERT INTO bridge_requests (team_pk, kind, args_json, response_json, status) "
                "VALUES (?, 'create_team', ?, ?, ?)",
                (
                    team_pk,
                    json.dumps({"name": name, "members": []}),
                    json.dumps({"team_id": team_id}),
                    status,
                ),
            )
        else:
            cur = con.execute(
                "INSERT INTO bridge_requests "
                "(team_pk, kind, args_json, response_json, status, created_at) "
                "VALUES (?, 'create_team', ?, ?, ?, ?)",
                (
                    team_pk,
                    json.dumps({"name": name, "members": []}),
                    json.dumps({"team_id": team_id}),
                    status,
                    created_at,
                ),
            )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _seed_team_delete(db, team_pk: str, team_id: str, *, status: str = "ready") -> int:
    """INSERT a `team_delete` teardown marker keyed on team_id (the symmetric
    subtractor of the orphan-join)."""
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (team_pk, kind, args_json, response_json, status) "
            "VALUES (?, 'team_delete', ?, '{}', ?)",
            (team_pk, json.dumps({"team_id": team_id}), status),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _seed_team_row(db, team_id: str, *, status: str) -> None:
    """INSERT a `teams` row. `project_id` + `lead_role` are NOT NULL (003)."""
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO teams (team_id, project_id, lead_role, status, schema_version) "
            "VALUES (?, 'p', 'atelier-pm-1', ?, 1)",
            (team_id, status),
        )
        con.commit()
    finally:
        con.close()


def _count_aborted_rows(db) -> int:
    con = sqlite3.connect(db)
    try:
        return int(
            con.execute("SELECT COUNT(*) FROM bridge_requests WHERE kind = 'aborted'").fetchone()[0]
        )
    finally:
        con.close()


# ── find_orphan_team_ids ────────────────────────────────────────────────────


def test_orphan_detected_when_team_delete_missing(workspace):
    """A ready `create_team` with NO matching `team_delete` (and team not
    closed) is an orphan; one WITH a matching `team_delete` is not. Returns
    `(team_pk, team_id)` pairs sourced from the create_team row."""
    db = workspace["db"]
    _seed_create_team(db, "run-1", "x", "team-x")  # orphan: no delete
    _seed_create_team(db, "run-2", "y", "team-y")  # matched: has delete
    _seed_team_delete(db, "run-2", "team-y")

    orphans = find_orphan_team_ids(db)
    assert orphans == [("run-1", "team-x")]


def test_matched_create_and_team_delete_is_not_an_orphan(workspace):
    """The `team_delete` NOT IN exclusion (filter i): a create with a matching
    ready `team_delete` is fully subtracted.

    ANTI-REVERT: if the `c.team_id NOT IN (SELECT ... FROM deleted ...)`
    exclusion is removed from the orphan SQL, `team-x` would (wrongly) be
    reported and this would FAIL."""
    db = workspace["db"]
    _seed_create_team(db, "run-1", "x", "team-x")
    _seed_team_delete(db, "run-1", "team-x")
    assert find_orphan_team_ids(db) == []


def test_closed_team_status_suppresses_orphan(workspace):
    """Filter (ii), the forward-safe hedge: a `create_team` whose team reached
    `teams.status='closed'` is NOT an orphan even with NO `team_delete` row.

    ANTI-REVERT: if the `c.team_id NOT IN (SELECT ... FROM closed ...)`
    exclusion is removed, `team-c` would (wrongly) be reported and this would
    FAIL. This is the assertion that breaks if the closed-team SQL exclusion is
    reverted."""
    db = workspace["db"]
    _seed_create_team(db, "run-1", "c", "team-c")  # no team_delete row at all
    _seed_team_row(db, "team-c", status="closed")
    assert find_orphan_team_ids(db) == []


def test_active_team_status_does_not_suppress_orphan(workspace):
    """The hedge subtracts ONLY closed teams — an `active` team row must NOT
    suppress the orphan (else the closed-only filter would be too broad). This
    makes test_closed_team_status_suppresses_orphan non-vacuous: the suppression
    is keyed specifically on `status='closed'`, not on mere row existence."""
    db = workspace["db"]
    _seed_create_team(db, "run-1", "a", "team-a")
    _seed_team_row(db, "team-a", status="active")
    assert find_orphan_team_ids(db) == [("run-1", "team-a")]


def test_old_create_team_excluded_by_7_day_window(workspace):
    """The orphan CTE filters `create_team` rows by
    `created_at >= datetime('now','-7 days')`. A 10-day-old orphan must NOT
    appear; a 1-day-old orphan must."""
    db = workspace["db"]
    _seed_create_team(db, "run-old", "old", "team-old")
    _seed_create_team(db, "run-new", "new", "team-new")
    # created_at must be a real ISO literal, not a SQL expr, so compute the
    # relative timestamps via the DB after the rows exist.
    _set_created_at(db, "team-old", "-10 days")
    _set_created_at(db, "team-new", "-1 days")

    orphans = find_orphan_team_ids(db)
    team_ids = [tid for _, tid in orphans]
    assert "team-new" in team_ids
    assert "team-old" not in team_ids, "orphans older than 7 days must be excluded"


def test_7_day_window_is_format_exact_at_the_boundary(workspace):
    """The window comparison is a LEXICAL TEXT compare; stored created_at is the
    'T'-separated trailing-'Z' form. If the threshold used the space-separated
    `datetime('now','-7 days')` form, a row just OVER 7 days old (real-time
    excluded) whose 'T'-form string lexically sorts as within-window would be
    WRONGLY included — the 'T' (0x54) vs space (0x20) skew at index 10.

    We seed a create_team that is real-time ~7 days + 1 hour old (genuinely
    OUTSIDE the window) and assert it is excluded. With the buggy space-format
    threshold this row would string-compare as within-window for part of the day
    and be wrongly reported; the format-matched strftime threshold excludes it.

    ANTI-REVERT: revert the threshold to `datetime('now','-7 days')` and this
    boundary row leaks back in for time-of-day windows where 'T' >= space.
    """
    db = workspace["db"]
    _seed_create_team(db, "run-edge", "edge", "team-edge")
    # ~7 days + 1 hour old: genuinely past the 7-day cutoff in real time.
    _set_created_at(db, "team-edge", "-7 days", "-1 hour")

    orphans = find_orphan_team_ids(db)
    team_ids = [tid for _, tid in orphans]
    assert "team-edge" not in team_ids, (
        "a row genuinely older than 7 days must be excluded regardless of the "
        "created_at timestamp format (T-separated vs space-separated)"
    )


def test_errored_create_team_is_not_an_orphan(workspace):
    """Only `status='ready'` create_team rows are orphan candidates. An errored
    (`status='error'`) create_team — even within the 7-day window and with no
    `team_delete` — is NOT treated as an orphan (an errored TeamCreate never
    minted a real team to leak)."""
    db = workspace["db"]
    _seed_create_team(db, "run-err", "boom", "team-err", status="error")
    assert find_orphan_team_ids(db) == []


# ── migration 009: AUTOINCREMENT high-water mark preserved ──────────────────


def test_migration_009_preserves_autoincrement_high_water_mark(tmp_path):
    """Migration 009 rebuilds bridge_requests (to widen the kind CHECK enum). A
    naive `INSERT ... SELECT *` rebuild re-seeds sqlite_sequence from MAX(id) of
    the COPIED rows, so a previously-allocated-then-deleted id would be REUSED
    after the rebuild — breaking the schema's "AUTOINCREMENT monotonic FIFO,
    never reused" invariant (009 header / line 82). 009 carries the old
    high-water mark forward.

    To bind 009's OWN behavior we apply migrations through 008 ONLY (009 is
    pre-marked applied so apply_migrations skips it), allocate ids 1..3 in the
    pre-009 table, DELETE id 3 (so MAX(id)=2 < seq=3 — the exact condition that
    exposes a naive rebuild's reuse), THEN run 009's executescript and assert the
    next insert gets id 4 (NOT the retired 3).

    ANTI-REVERT: drop the `UPDATE sqlite_sequence ...` step from 009 and the
    rebuild re-seeds the sequence to 2, so the next insert reuses id 3 — FAILS.
    """
    db = str(tmp_path / "atelier-pre009.db")
    shared = MIGRATIONS_DIR / "shared"

    # Apply through 008 only: pre-record 009 as applied so apply_migrations
    # skips it, leaving the pre-009 (008) bridge_requests table in place.
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS migrations "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO migrations (filename, applied_at) VALUES "
        "('009_bridge_requests_aborted_kind.sql', '2026-01-01T00:00:00Z')"
    )
    con.commit()
    con.close()
    apply_migrations(db, shared)  # applies 001-008; 009 skipped as pre-applied

    con = sqlite3.connect(db)
    try:
        # Seed ids 1..3 in the pre-009 table, then retire id 3.
        for pk in ("a", "b", "c"):
            con.execute(
                "INSERT INTO bridge_requests (team_pk, kind, args_json, status) "
                "VALUES (?, 'create_team', '{}', 'pending')",
                (pk,),
            )
        con.commit()
        assert (
            con.execute("SELECT seq FROM sqlite_sequence WHERE name='bridge_requests'").fetchone()[
                0
            ]
            == 3
        )
        con.execute("DELETE FROM bridge_requests WHERE id = 3")
        con.commit()

        # Now run 009's rebuild (the real migration SQL).
        sql_009 = (shared / "009_bridge_requests_aborted_kind.sql").read_text()
        con.executescript(sql_009)
        con.commit()

        # The retired id MUST NOT be reused: 009 preserved the high-water mark.
        con.execute(
            "INSERT INTO bridge_requests (team_pk, kind, args_json, status) "
            "VALUES ('d', 'aborted', '{}', 'pending')"
        )
        con.commit()
        next_id = con.execute("SELECT id FROM bridge_requests WHERE team_pk='d'").fetchone()[0]
        assert next_id == 4, (
            f"retired id was reused (got {next_id}); 009 must preserve sqlite_sequence"
        )
    finally:
        con.close()


# ── enqueue_aborted_row ─────────────────────────────────────────────────────


def test_enqueue_aborted_row_writes_team_ids_at_risk(workspace):
    db = workspace["db"]
    row_id = enqueue_aborted_row(db, "run-9", ["team-a", "team-b"])
    con = sqlite3.connect(db)
    try:
        team_pk, kind, args_json, status = con.execute(
            "SELECT team_pk, kind, args_json, status FROM bridge_requests WHERE id=?",
            (row_id,),
        ).fetchone()
    finally:
        con.close()
    assert team_pk == "run-9"
    assert kind == "aborted"
    assert status == "pending"
    parsed = json.loads(args_json)
    assert parsed["team_ids_at_risk"] == ["team-a", "team-b"]
    assert "reason" in parsed


# ── main() — returns 0 on all paths ─────────────────────────────────────────


def test_main_returns_zero_when_no_orphans(workspace, capsys):
    """No orphan rows at all → main returns 0 (a failing sweep must never fail
    the run that calls it) and enqueues nothing."""
    db = workspace["db"]
    rc = main(["--bridge-db", db])
    assert rc == 0
    assert _count_aborted_rows(db) == 0


def test_main_print_path_returns_zero_without_enqueueing(workspace, capsys):
    """With orphans but NO --team-pk, main prints the list and returns 0 WITHOUT
    enqueuing an aborted row."""
    db = workspace["db"]
    _seed_create_team(db, "run-1", "x", "team-x")
    rc = main(["--bridge-db", db])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run-1\tteam-x" in out
    assert _count_aborted_rows(db) == 0


def test_main_enqueue_path_writes_exactly_one_aborted_row(workspace):
    """The headline contract: an orphan with NO matching `team_delete` and team
    not closed → `main(--team-pk RUN)` enqueues EXACTLY ONE `aborted` row whose
    `args_json.team_ids_at_risk` == the orphan team_ids, and returns 0."""
    db = workspace["db"]
    # Two orphans for one matched delete → exactly the two leaked ids enqueued.
    _seed_create_team(db, "run-1", "x", "team-x")  # orphan
    _seed_create_team(db, "run-2", "y", "team-y")  # orphan
    _seed_create_team(db, "run-3", "z", "team-z")  # matched
    _seed_team_delete(db, "run-3", "team-z")

    rc = main(["--bridge-db", db, "--team-pk", "sweep-run"])
    assert rc == 0

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT team_pk, args_json, status FROM bridge_requests WHERE kind = 'aborted'"
        ).fetchall()
    finally:
        con.close()

    # EXACTLY ONE aborted row.
    assert len(rows) == 1
    team_pk, args_json, status = rows[0]
    assert team_pk == "sweep-run"
    assert status == "pending"
    at_risk = json.loads(args_json)["team_ids_at_risk"]
    # team_ids_at_risk == the orphan ids exactly — the matched team-z is absent.
    assert sorted(at_risk) == ["team-x", "team-y"]
    assert "team-z" not in at_risk


def test_main_enqueue_path_returns_zero_with_no_orphans(workspace):
    """Even with --team-pk, an empty orphan set short-circuits to return 0 and
    enqueues nothing (no futile aborted row)."""
    db = workspace["db"]
    _seed_create_team(db, "run-1", "x", "team-x")
    _seed_team_delete(db, "run-1", "team-x")  # matched → no orphans
    rc = main(["--bridge-db", db, "--team-pk", "sweep-run"])
    assert rc == 0
    assert _count_aborted_rows(db) == 0


# ── helper: set created_at to a DB-computed relative timestamp ──────────────


def _set_created_at(db, team_id: str, *deltas: str) -> None:
    """Rewrite the create_team row's created_at to the PRODUCTION storage format
    `strftime('%Y-%m-%dT%H:%M:%fZ','now',*deltas)` — the SAME 'T'-separated,
    trailing-'Z' ISO form the 008/009 schema default writes. Seeding the real
    stored format (NOT the space-separated `datetime('now',delta)` form) so the
    window comparison is exercised against production-shaped timestamps and the
    boundary skew cannot hide. Accepts one or more SQLite date modifiers, e.g.
    `'-10 days'` or `'-7 days', '-1 hour'`."""
    # strftime takes the modifiers as trailing args; build a placeholder list so
    # the SQL stays fully bound (no interpolation).
    placeholders = ", ".join("?" for _ in deltas)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE bridge_requests "
            f"SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', {placeholders}) "
            "WHERE kind = 'create_team' "
            "AND json_extract(response_json, '$.team_id') = ?",
            (*deltas, team_id),
        )
        con.commit()
    finally:
        con.close()
