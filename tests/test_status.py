"""Pytest suite for ``scripts/status.py`` — the read-only team-mode run-status
reporter (atelier#65, AC#5).

``render_status`` is a PURE text renderer over durable Local-mode state. These
tests stand up a real Local-mode SQLite DB with every migration applied
(mirroring ``tests/test_pm_dispatch.py`` / ``tests/test_backend_local_state.py``),
seed a MID-RUN snapshot directly — a ``teams`` row, a ``team_members`` roster,
``tasks`` across two waves (wave 0 fully terminal, wave 1 with in-flight workers
that have ``attempts>0`` and a fresh ``last_attempt_at``), and ``bridge_messages``
``reply`` rows carrying valid TM-006 envelopes — and assert the rendered text
answers the three operator questions correctly:

* ACTIVE WAVE NUMBER — the lowest non-all-terminal wave's ``parallel_group``.
* IN-FLIGHT WORKER COUNT — active-wave tasks that are non-terminal, dispatched
  (``attempts>0``), and within the per-attempt wall-clock.
* LATEST ENVELOPES — the newest valid reply per roster recipient, with each
  artifact preview TRUNCATED (a long artifact MUST be cut).

Read-only contract: ``render_status`` PEEKs the bridge with
``read_once(..., update_cursor=False)`` — the ``bridge_delivery`` cursor MUST NOT
advance. We assert this two ways: (a) directly verifying the cursor row is
untouched after render, and (b) patching ``read_once`` and asserting the
``update_cursor=False`` kwarg.

Tests are NON-VACUOUS: each asserts on a specific computed value (wave number,
in-flight count, truncation marker) that a reverted implementation would get
wrong, and several pin EXACT counts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts import status as status_mod
from scripts.migrate import apply_migrations
from scripts.pm_dispatch import WALL_CLOCK_S
from scripts.status import main, render_status

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

# Stable identifiers for the seeded mid-run snapshot.
_TEAM_ID = "team-65"
_TEAM_PK = "run-65-cycle-1"
_LEAD_ROLE = "atelier-pm-1"
_WORKER_A = "backend-engineer-1"
_WORKER_B = "sdet-1"


def _iso(dt: datetime) -> str:
    """ISO-8601 UTC with the trailing-Z form ``status._parse_ts`` consumes.

    ``_parse_ts`` strips the ``Z`` and feeds the rest to
    ``datetime.fromisoformat``, so the body MUST be a real ISO timestamp
    (``YYYY-MM-DDTHH:MM:SS[.ffffff]``)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── Local-mode DB fixture (mirrors tests/test_pm_dispatch.py) ────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace.

    Identical bootstrap to ``tests/test_pm_dispatch.py``: chdir into a fake git
    root, apply all shared + local-only migrations (so ``PRAGMA user_version``
    == 1, which ``bridge_read.read_once`` enforces), force ``detect_mode`` to
    ``local``, and seed one workspace + project so tasks have a parent.
    """
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    db = root / ".ai" / "atelier.db"
    db.parent.mkdir()
    apply_migrations(str(db), MIGRATIONS_DIR / "shared")
    apply_migrations(str(db), MIGRATIONS_DIR / "local-only")

    now = "2026-05-29T00:00:00Z"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO workspaces (slug, identity, name, description, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("proj", "repo:proj", "Proj", None, now, now),
    )
    ws_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, "
        "phase, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "p", "P", "d", "design:open", "atelier-pm-1", now, now),
    )
    proj_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"root": root, "db": str(db), "project_id": proj_id}


# ── Seed helpers ────────────────────────────────────────────────────────────


def _seed_task(
    workspace,
    *,
    title,
    parallel_group,
    status="pending",
    attempts=0,
    last_attempt_at=None,
    created_at="2026-05-29T00:00:00Z",
    team_pk=None,
):
    """Insert one task row with explicit dispatch-state columns (006).

    ``team_pk`` (010) is the run/cycle correlation id; NULL by default to mirror
    a legacy/pre-010 row. The per-cycle status scoping filters on this column
    when present.
    """
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.execute(
        "INSERT INTO tasks (project_id, title, description, status, "
        "parallel_group, attempts, last_attempt_at, team_pk, created_by, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace["project_id"],
            title,
            "d",
            status,
            parallel_group,
            attempts,
            last_attempt_at,
            team_pk,
            "atelier-pm-1",
            created_at,
            created_at,
        ),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _seed_team_and_roster(workspace, *, roles):
    """Seed teams + a persona_snapshot + team_members for ``roles``.

    ``bridge_messages`` carries a composite FK ``(team_id, sender_id)`` ->
    ``team_members(team_id, role_id)`` plus a NOT-NULL FK to
    ``persona_snapshots(id)``, so every recipient/sender we later reference must
    exist as a member with a pinned snapshot. Returns the persona_snapshot_id.
    """
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status, "
        "schema_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            _TEAM_ID,
            str(workspace["project_id"]),
            _LEAD_ROLE,
            "active",
            1,
            "2026-05-29T00:00:00Z",
        ),
    )
    cur = conn.execute(
        "INSERT INTO persona_snapshots (persona_version, persona_blob, created_at) "
        "VALUES (?, ?, ?)",
        ("v1", "{}", "2026-05-29T00:00:00Z"),
    )
    snap_id = cur.lastrowid
    for role in roles:
        conn.execute(
            "INSERT INTO team_members (team_id, role_id, member_name, wave, "
            "persona_snapshot_id, joined_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_TEAM_ID, role, role, 0, snap_id, "2026-05-29T00:00:00Z"),
        )
    conn.commit()
    conn.close()
    return snap_id


def _envelope(task_id, *, status="done", artifacts, notes_md="impl complete"):
    """A valid TM-006 reply envelope (attempt fixed at 1)."""
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": 1,
        "status": status,
        "artifacts": artifacts,
        "notes_md": notes_md,
        "next_action": "review",
    }


def _seed_reply(workspace, *, recipient, sender, snap_id, seq, envelope):
    """Append one ``reply`` bridge_messages row delivered to ``recipient``.

    ``status.py`` peeks each roster recipient's inbox, so the envelope we want
    surfaced for a role rides as a ``reply`` whose ``recipient`` is that role.
    Payload is the RAW envelope JSON (bridge_send writes raw; bridge_read fences
    on read; ``_parse_reply_envelope`` strips the fence + parses).
    """
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO bridge_messages (team_id, recipient, seq, sender_id, "
        "kind, wave, payload, persona_snapshot_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _TEAM_ID,
            recipient,
            seq,
            sender,
            "reply",
            1,
            json.dumps(envelope),
            snap_id,
            "2026-05-29T00:00:01Z",
        ),
    )
    conn.commit()
    conn.close()


def _delivery_cursor(workspace, recipient):
    """Return the bridge_delivery.last_seq for ``recipient`` or ``None``."""
    conn = sqlite3.connect(workspace["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_seq FROM bridge_delivery WHERE team_id=? AND recipient=?",
        (_TEAM_ID, recipient),
    ).fetchone()
    conn.close()
    return None if row is None else int(row["last_seq"])


# ── The canonical mid-run snapshot ──────────────────────────────────────────


@pytest.fixture
def mid_run(workspace):
    """A realistic mid-run snapshot.

    * Wave 0: TWO tasks, BOTH ``complete`` (terminal) — so wave 0 is dropped by
      ``partition_waves`` and the active wave is wave 1.
    * Wave 1: THREE tasks — two in-flight (non-terminal, ``attempts>0``, fresh
      ``last_attempt_at``) and one ``pending`` with ``attempts==0`` (dispatched
      zero times → NOT in-flight). So the in-flight count is EXACTLY 2.
    * Roster: lead + two workers; each worker has a valid reply envelope, the
      ``backend-engineer-1`` one carrying a LONG artifact path so the render must
      truncate it.
    """
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))

    # Wave 0 — fully terminal.
    _seed_task(workspace, title="w0a", parallel_group=0, status="complete", attempts=1)
    _seed_task(workspace, title="w0b", parallel_group=0, status="complete", attempts=1)

    # Wave 1 — the active wave: 2 in-flight + 1 not-yet-dispatched.
    t_inflight_a = _seed_task(
        workspace,
        title="w1a",
        parallel_group=1,
        status="in_progress",
        attempts=2,
        last_attempt_at=fresh,
    )
    t_inflight_b = _seed_task(
        workspace,
        title="w1b",
        parallel_group=1,
        status="pending",
        attempts=1,
        last_attempt_at=fresh,
    )
    # w1c carries a FRESH last_attempt_at but attempts==0 (never actually
    # dispatched) — so ONLY the attempts>0 guard keeps it out of the in-flight
    # count. This makes the count test bind that specific guard.
    _seed_task(
        workspace,
        title="w1c",
        parallel_group=1,
        status="pending",
        attempts=0,
        last_attempt_at=fresh,
    )

    snap_id = _seed_team_and_roster(workspace, roles=[_LEAD_ROLE, _WORKER_A, _WORKER_B])

    long_path = "src/very/deeply/nested/module/" + ("a" * 400) + "/file.py"
    _seed_reply(
        workspace,
        recipient=_WORKER_A,
        sender=_LEAD_ROLE,
        snap_id=snap_id,
        seq=1,
        envelope=_envelope(
            t_inflight_a,
            artifacts=[{"path": long_path, "sha": "deadbeef"}],
        ),
    )
    _seed_reply(
        workspace,
        recipient=_WORKER_B,
        sender=_LEAD_ROLE,
        snap_id=snap_id,
        seq=1,
        envelope=_envelope(
            t_inflight_b,
            artifacts=[{"path": "tests/test_x.py", "sha": "cafef00d"}],
            notes_md="tests green",
        ),
    )
    return {
        "long_path": long_path,
        "t_inflight_a": t_inflight_a,
        "t_inflight_b": t_inflight_b,
    }


# ── (1) active wave number ──────────────────────────────────────────────────


def test_render_reports_lowest_non_terminal_wave_number(workspace, mid_run):
    """The active wave is wave 1 — wave 0 is fully terminal and dropped. The
    rendered text MUST name wave number 1 (NOT 0)."""
    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)

    assert "active wave: 1" in text
    # Non-vacuous: wave 0 is terminal, so it must NOT be reported as active.
    assert "active wave: 0" not in text
    # The active wave has 3 tasks (the whole wave, in-flight or not).
    assert "active wave: 1 (3 task(s) in wave)" in text


def test_render_picks_lowest_of_multiple_live_waves(workspace):
    """With TWO simultaneously-live waves, the active wave is the LOWEST — wave 0,
    not wave 1. This binds ``_active_wave``'s ``waves[0]`` (lowest) ordering: a
    regression that surfaced ``waves[-1]`` (or any non-lowest live wave) would
    report wave 1 and FAIL here.

    The ``mid_run`` fixture cannot exercise this — there wave 0 is fully terminal,
    leaving exactly ONE live wave, so ``waves[0]`` and ``waves[-1]`` coincide.
    Here BOTH waves carry a live task, so the choice is observable.
    """
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    # Wave 0 — has a LIVE (non-terminal) task, so it is NOT dropped.
    _seed_task(
        workspace,
        title="w0-live",
        parallel_group=0,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    # Wave 1 — also has a live task.
    _seed_task(
        workspace,
        title="w1-live",
        parallel_group=1,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    # The LOWEST live wave (0) is active …
    assert "active wave: 0" in text
    # … NOT the highest (1) — this is the property the old fixture could not bind.
    assert "active wave: 1" not in text


# ── (2) in-flight worker count ──────────────────────────────────────────────


def test_render_reports_exact_in_flight_worker_count(workspace, mid_run):
    """EXACTLY 2 of the 3 wave-1 tasks are in-flight: the two with
    ``attempts>0`` + a fresh ``last_attempt_at``. The ``attempts==0`` task is
    NOT counted (never dispatched)."""
    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    assert "in-flight workers: 2" in text
    # Non-vacuous guard against an off-by-one / count-all-tasks regression.
    assert "in-flight workers: 3" not in text
    assert "in-flight workers: 0" not in text


def test_stale_last_attempt_is_not_counted_in_flight(workspace):
    """A task past the per-attempt wall-clock is NOT in-flight (the scheduler
    will soft-kill it). One fresh + one stale wave-1 worker → count is 1, not
    2 — binding to ``WALL_CLOCK_S`` (imported, not hardcoded)."""
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    stale = _iso(datetime.now(timezone.utc) - timedelta(seconds=WALL_CLOCK_S + 600))
    _seed_task(
        workspace,
        title="fresh",
        parallel_group=1,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    _seed_task(
        workspace,
        title="stale",
        parallel_group=1,
        status="in_progress",
        attempts=3,
        last_attempt_at=stale,
    )
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    assert "in-flight workers: 1" in text
    assert "in-flight workers: 2" not in text


# ── (3) latest envelopes + truncation ───────────────────────────────────────


def test_render_shows_latest_envelopes_with_truncated_artifact(workspace, mid_run):
    """The latest valid envelope per recipient is rendered; a LONG artifact path
    is TRUNCATED to the preview cap (the ``…(+N more)`` marker is present and the
    full untruncated path is NOT)."""
    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)

    # Both workers' envelopes surfaced under their role headers.
    assert f"[{_WORKER_A}] status=done" in text
    assert f"[{_WORKER_B}] status=done" in text
    # The short artifact appears verbatim …
    assert "tests/test_x.py" in text
    # … and the long one is truncated: the ellipsis marker is present.
    assert "…(+" in text
    # The FULL 400-char path must NOT appear intact (proves truncation happened).
    assert mid_run["long_path"] not in text
    # And no rendered line exceeds the cap by a wide margin (preview is capped).
    long_lines = [ln for ln in text.splitlines() if "…(+" in ln]
    assert long_lines, "expected at least one truncated artifact line"
    # The preview body is bounded by the module cap (+ the marker + indent).
    assert len(long_lines[0]) < status_mod._ARTIFACT_PREVIEW_CAP + 60


def test_truncation_marker_reports_elided_length(workspace, mid_run):
    """The truncation marker reports HOW MANY chars were elided — a non-vacuous
    check that the preview is genuinely partial, not just cosmetically clipped.

    The artifact renders as ``"{path} (sha={sha})"`` and is cut at the cap, so
    the elided count is measured against that rendered form (not the bare path).
    We derive the exact rendered preview via the module's own ``_render_artifact``
    so the expectation pins the real contract."""
    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    rendered = status_mod._render_artifact({"path": mid_run["long_path"], "sha": "deadbeef"})
    # The rendered preview ends in the explicit elision marker …
    assert rendered.endswith(" more)")
    assert "…(+" in rendered
    # … and it appears verbatim in the snapshot.
    assert rendered in text
    # The elided count is the rendered (path + sha-suffix) length minus the cap.
    full = f"{mid_run['long_path']} (sha=deadbeef)"
    elided = len(full) - status_mod._ARTIFACT_PREVIEW_CAP
    assert f"…(+{elided} more)" in text


def test_deep_inbox_surfaces_newest_reply_beyond_read_limit(workspace):
    """When a recipient's inbox is deeper than ``read_once``'s default limit, the
    GENUINELY-newest reply (seq beyond the oldest 500) must still be surfaced.

    ``read_once`` returns the OLDEST ``DEFAULT_LIMIT`` rows (``seq > since_seq
    ORDER BY seq ASC LIMIT``). Peeking from ``since_seq=0`` on a >500-row inbox
    would scan only the oldest window and MISS the newest reply. ``status`` seeks
    the inbox TAIL (``max_seq - DEFAULT_LIMIT``) so the newest reply is always in
    the page.

    We seed ``DEFAULT_LIMIT + 1`` early heartbeat/reply rows at low seqs plus one
    fresh ``done`` reply at the highest seq, then assert the snapshot renders the
    newest reply's envelope (its unique notes marker).

    ANTI-REVERT: revert the tail-seek (peek from ``since_seq=0``) and the newest
    reply at the high seq falls outside the oldest-500 window — FAILS.
    """
    from scripts.bridge_read import DEFAULT_LIMIT

    snap_id = _seed_team_and_roster(workspace, roles=[_LEAD_ROLE, _WORKER_A])
    t = _seed_task(workspace, title="w", parallel_group=1, status="in_progress", attempts=1)

    # Bulk-seed DEFAULT_LIMIT old reply rows (seq 1..DEFAULT_LIMIT) for WORKER_A.
    conn = sqlite3.connect(workspace["db"])
    conn.execute("PRAGMA foreign_keys=ON")
    old_env = _envelope(t, artifacts=[], notes_md="OLD reply")
    conn.executemany(
        "INSERT INTO bridge_messages (team_id, recipient, seq, sender_id, "
        "kind, wave, payload, persona_snapshot_id, created_at) "
        "VALUES (?, ?, ?, ?, 'reply', 1, ?, ?, ?)",
        [
            (
                _TEAM_ID,
                _WORKER_A,
                seq,
                _LEAD_ROLE,
                json.dumps(old_env),
                snap_id,
                "2026-05-29T00:00:01Z",
            )
            for seq in range(1, DEFAULT_LIMIT + 1)
        ],
    )
    conn.commit()
    conn.close()

    # The NEWEST reply, at the highest seq (beyond a from-zero oldest-500 window).
    newest_env = _envelope(t, artifacts=[{"path": "newest.py"}], notes_md="NEWEST reply marker")
    _seed_reply(
        workspace,
        recipient=_WORKER_A,
        sender=_LEAD_ROLE,
        snap_id=snap_id,
        seq=DEFAULT_LIMIT + 1,
        envelope=newest_env,
    )

    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    # The newest reply's unique marker is surfaced …
    assert "NEWEST reply marker" in text
    # … and the stale OLD reply is NOT what gets shown for this recipient.
    worker_section = text.split(f"[{_WORKER_A}]", 1)[1]
    assert "OLD reply" not in worker_section.split("[", 1)[0]


# ── read-only cursor invariant (the load-bearing contract) ──────────────────


def test_render_does_not_advance_delivery_cursor(workspace, mid_run):
    """``render_status`` PEEKs the bridge — it MUST NOT advance the per-recipient
    delivery cursor (that would hide the reply from the real consumer). After a
    render, ``bridge_delivery`` has NO row for either worker (the peek never
    upserted a cursor)."""
    # Pre-condition: no cursor exists yet.
    assert _delivery_cursor(workspace, _WORKER_A) is None
    assert _delivery_cursor(workspace, _WORKER_B) is None

    render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)

    # The cursor MUST remain unset — a read with update_cursor=True would have
    # upserted last_seq=1 for each recipient.
    assert _delivery_cursor(workspace, _WORKER_A) is None
    assert _delivery_cursor(workspace, _WORKER_B) is None


def test_render_passes_update_cursor_false_to_read_once(workspace, mid_run, monkeypatch):
    """Belt-and-braces: patch ``read_once`` and assert EVERY call passes
    ``update_cursor=False``. This pins the kwarg at the call site even if the
    cursor side-effect were ever refactored."""
    seen_kwargs = []
    real_read_once = status_mod.read_once

    def spy(db_path, **kwargs):
        seen_kwargs.append(kwargs)
        return real_read_once(db_path, **kwargs)

    monkeypatch.setattr(status_mod, "read_once", spy)

    render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)

    # read_once was invoked at least once (per roster recipient) …
    assert seen_kwargs, "render_status never peeked the bridge"
    # … and EVERY invocation peeked with update_cursor=False.
    assert all(kw.get("update_cursor") is False for kw in seen_kwargs)


# ── graceful empty / all-terminal rendering ─────────────────────────────────


def test_all_terminal_run_renders_complete_without_crashing(workspace):
    """When every task is terminal, the active wave is ``None`` and the report
    says the run is complete — no crash, in-flight count 0."""
    _seed_task(workspace, title="done0", parallel_group=0, status="complete", attempts=1)
    _seed_task(workspace, title="done1", parallel_group=1, status="abandoned", attempts=2)
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    assert "all tasks terminal" in text
    assert "in-flight workers: 0" in text


def test_no_tasks_renders_gracefully(workspace):
    """A team with no tasks at all renders the 'no tasks' line, count 0, and an
    empty-roster envelope notice — never raising."""
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])
    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    assert "(no tasks for this team)" in text
    assert "in-flight workers: 0" in text


def test_live_null_parallel_group_task_does_not_crash_render(workspace):
    """A live (non-terminal) task with a NULL ``parallel_group`` co-existing with
    an int-grouped live task MUST NOT crash the render.

    ``partition_waves`` sorts by ``parallel_group`` with a bare subscript and a
    NULL beside an int raises ``TypeError`` — but ``status`` deliberately skips
    ``preflight_validate`` (it must be safe to run any time, even mid-plan before
    NULL groups are filled in). The ungrouped task is segregated and surfaced in
    its own line; the int wave still renders as the active wave.

    ANTI-REVERT: if ``_active_wave`` stops filtering NULL-group tasks before
    ``partition_waves``, ``render_status`` raises ``TypeError`` and this FAILS.
    """
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    # One live NULL-parallel_group task …
    _seed_task(
        workspace,
        title="ungrouped",
        parallel_group=None,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    # … co-existing with a live int-grouped task in the same project.
    _seed_task(
        workspace,
        title="grouped",
        parallel_group=2,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    # The int wave still renders as the active wave (NULL excluded, not crashing).
    assert "active wave: 2" in text
    # The ungrouped task is surfaced explicitly (exactly one).
    assert "ungrouped tasks (NULL parallel_group): 1" in text


def test_all_grouped_terminal_but_live_null_group_remains(workspace):
    """When every GROUPED task is terminal but a live NULL-group task remains,
    the run is NOT complete — the active-wave line must say so (not claim the run
    finished) and the ungrouped task must be surfaced.

    Guards against the NULL-filter being applied so aggressively that a live
    NULL-group task is mistaken for an empty (complete) run.
    """
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    _seed_task(workspace, title="g0", parallel_group=0, status="complete", attempts=1)
    _seed_task(
        workspace,
        title="ungrouped",
        parallel_group=None,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk=_TEAM_PK)
    assert "no grouped live tasks" in text
    # Must NOT claim the run is complete while a live task remains.
    assert "run complete" not in text
    assert "ungrouped tasks (NULL parallel_group): 1" in text


def test_missing_team_renders_without_crashing(workspace):
    """An unknown team_id (no teams row, no roster) degrades to empty sections —
    render_status never raises even when the bridge peek would 404 the team."""
    text = render_status(workspace["db"], team_id="ghost-team", team_pk="ghost-pk")
    assert "(no tasks for this team)" in text
    assert "(no team members on the roster)" in text


# ── (2b) per-cycle team_pk scoping (010 / atelier#90 part 2) ─────────────────


def test_status_scopes_by_team_pk_not_project(workspace):
    """When ONE project hosts TWO concurrent cycles (distinct team_pk values)
    with DISTINCT wave shapes, ``render_status(team_pk='run-A')`` reports ONLY
    run-A's active wave + in-flight count — NOT the project-wide conflated sum.

    Pre-010 (no team_pk column / ``_tasks_for_team`` ignores team_pk) this fails:
    the snapshot scopes by project, so run-A's render would surface run-B's wave
    and count BOTH cycles' in-flight workers.

    NON-VACUITY (this is the load-bearing detail): run-A's active wave (wave 5)
    is HIGHER than run-B's active wave (wave 1). The active-wave number is the
    LOWEST non-terminal wave, so the PROJECT-WIDE active wave (if the team_pk
    predicate were dropped) would be wave 1 — run-B's — NOT run-A's wave 5. So
    a predicate that silently ignored team_pk would render run-A's snapshot as
    ``active wave: 1`` with run-B's ``in-flight workers: 2``, both DIFFERENT
    from the scoped run-A answer (wave 5, 1 in-flight). The run-A assertions
    below therefore genuinely bind the team_pk scoping rather than coinciding
    with the project-wide render.
    """
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))

    # run-A: wave 0 fully terminal, wave 5 active with EXACTLY 1 in-flight. Note
    # wave 5 is ABOVE run-B's active wave (1), so the project-wide active wave
    # (lowest non-terminal across BOTH cycles) is run-B's wave 1 — NOT wave 5.
    _seed_task(
        workspace, title="A-w0", parallel_group=0, status="complete", attempts=1, team_pk="run-A"
    )
    _seed_task(
        workspace,
        title="A-w5-live",
        parallel_group=5,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
        team_pk="run-A",
    )

    # run-B: a DISTINCT, LOWER wave shape — active wave is wave 1 with 2 in-flight.
    _seed_task(
        workspace,
        title="B-w1-live-1",
        parallel_group=1,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
        team_pk="run-B",
    )
    _seed_task(
        workspace,
        title="B-w1-live-2",
        parallel_group=1,
        status="in_progress",
        attempts=2,
        last_attempt_at=fresh,
        team_pk="run-B",
    )

    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    text_a = render_status(workspace["db"], team_id=_TEAM_ID, team_pk="run-A")
    # run-A's active wave is wave 5 with exactly 1 in-flight worker.
    assert "active wave: 5 (1 task(s) in wave)" in text_a
    assert "in-flight workers: 1" in text_a
    # Non-vacuous: the project-wide active wave would be run-B's wave 1 (lower)
    # if team_pk scoping were dropped — it must NOT leak into run-A's snapshot,
    # nor must run-B's in-flight count of 2.
    assert "active wave: 1" not in text_a
    assert "in-flight workers: 2" not in text_a
    assert "in-flight workers: 3" not in text_a
    # Header surfaces the cycle scope.
    assert "scope: cycle (team_pk)" in text_a

    text_b = render_status(workspace["db"], team_id=_TEAM_ID, team_pk="run-B")
    # run-B's active wave is wave 1 with exactly 2 in-flight workers.
    assert "active wave: 1 (2 task(s) in wave)" in text_b
    assert "in-flight workers: 2" in text_b
    assert "active wave: 5" not in text_b


def test_status_falls_back_to_project_scope_for_legacy_null_team_pk(workspace):
    """A project whose tasks ALL carry team_pk=NULL (legacy / pre-010 /
    single-cycle that never stamped) renders project-wide via the COUNT-probe
    fallback — identical numbers to the pre-fix project-scope path.

    Guards against the 'WHERE team_pk=? returns ZERO rows for legacy projects'
    regression: an unconditional team_pk predicate would render an empty
    snapshot for every pre-010 project.
    """
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    # All tasks NULL team_pk; active wave is wave 1 with 2 in-flight.
    _seed_task(workspace, title="w0", parallel_group=0, status="complete", attempts=1)
    _seed_task(
        workspace,
        title="w1a",
        parallel_group=1,
        status="in_progress",
        attempts=1,
        last_attempt_at=fresh,
    )
    _seed_task(
        workspace,
        title="w1b",
        parallel_group=1,
        status="in_progress",
        attempts=2,
        last_attempt_at=fresh,
    )
    _seed_team_and_roster(workspace, roles=[_LEAD_ROLE])

    # An arbitrary team_pk that matches ZERO rows must NOT empty the snapshot —
    # the COUNT-probe sees 0 team_pk matches and falls back to project scope.
    text = render_status(workspace["db"], team_id=_TEAM_ID, team_pk="run-legacy")
    assert "active wave: 1 (2 task(s) in wave)" in text
    assert "in-flight workers: 2" in text
    # The fallback path must announce project-wide scope, not cycle scope.
    assert "scope: project (team_pk unpopulated)" in text
    assert "scope: cycle (team_pk)" not in text


# ── CLI entrypoint ──────────────────────────────────────────────────────────


def test_main_returns_zero_in_local_mode(workspace, mid_run, capsys):
    """``main()`` prints the snapshot and returns 0 in Local mode."""
    rc = main(["--db", workspace["db"], "--team-id", _TEAM_ID, "--team-pk", _TEAM_PK])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== atelier run status ===" in out
    assert "active wave: 1" in out
    assert "in-flight workers: 2" in out


def test_main_returns_zero_and_skips_render_in_non_local_mode(workspace, monkeypatch, capsys):
    """In non-local mode ``main()`` prints the Local-mode notice and returns 0 —
    read-only, never rendering the (unpopulated) dispatch state."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")
    rc = main(["--db", workspace["db"], "--team-id", _TEAM_ID, "--team-pk", _TEAM_PK])
    assert rc == 0
    out = capsys.readouterr().out
    assert "requires Local mode" in out
    # Non-vacuous: the snapshot header must NOT be rendered in non-local mode.
    assert "=== atelier run status ===" not in out
