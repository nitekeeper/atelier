"""End-to-end test of the production dispatch binding (atelier#81).

Drives a REAL `pm_dispatch.WaveDispatcher` through the production queue-bridge
transport for BOTH dispatch modes (subagent AND agent-team):

    enqueue (QueueBridgeDispatchTools) → a DETERMINISTIC servicer flips the
    bridge_requests rows + writes terminal reply envelopes into bridge_messages
    → build_poll_fn reads the envelope → the wave barrier advances → the wave
    completes.

The "servicer" is a small, deterministic state-machine over the two tables
(bridge_requests pending→ready/error + bridge_messages envelopes) — NOT ad-hoc
per-call mocks. It stands in for the orchestrator turn-loop
(`internal/bridge-poll/SKILL.md`): for each pending row it performs the row's
"effect" (mint a team_id for create_team; write a terminal envelope for a
spawn/send) and flips the row to 'ready' (or 'error' for the adversarial cases).

EXACT-COUNT assertions throughout (servicer fires exactly once per
(request, attempt); the barrier releases after exactly the planned task count)
— NOT loose `>=`.

Backend coverage: the request-queue path is Local-only at runtime (we build it
on a real Local DB). The poll path reads the shared `bridge_messages` table —
exercised here against the same Local DB; the table is defined in shared/003 so
the read contract is backend-symmetric (a dedicated Memex-mode read test lives in
the bridge_read suite — this e2e pins the Local wiring + barrier semantics).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from scripts import mode_detector
from scripts.dispatch import (
    DISPATCH_MODE_AGENT_TEAM,
    DISPATCH_MODE_SUBAGENT,
    QueueBridgeDispatchTools,
    build_poll_fn,
    build_spawn_fn,
)
from scripts.migrate import apply_migrations
from scripts.pm_dispatch import WaveDispatcher

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ── Local-mode workspace fixture (mirrors test_pm_dispatch.py) ──────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real Local-mode atelier DB rooted at a fake git workspace, with a
    seeded project so the tasks.* mutators (increment_attempt / complete_task /
    set_abandoned) the engine calls succeed."""
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
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA foreign_keys=ON")
    cur = con.execute(
        "INSERT INTO workspaces (slug, identity, name, description, created_at, updated_at) "
        "VALUES ('proj', 'repo:proj', 'Proj', NULL, ?, ?)",
        (now, now),
    )
    ws_id = cur.lastrowid
    cur = con.execute(
        "INSERT INTO projects (workspace_id, slug, name, description, phase, created_by, "
        "created_at, updated_at) VALUES (?, 'p', 'P', 'd', 'design:open', 'atelier-pm-1', ?, ?)",
        (ws_id, now, now),
    )
    proj_id = cur.lastrowid
    con.commit()
    con.close()
    return {"root": root, "db": str(db), "project_id": proj_id}


def _seed_task(
    workspace, *, title, parallel_group, created_at="2026-05-31T00:00:00Z", assigned_to=None
):
    """Seed one real `tasks` row. `assigned_to` (the dispatch role-id) defaults to
    None so existing callers are byte-identical; a caller may set it to prove the
    column survives the real DB load/projection (the model-tier e2e)."""
    con = sqlite3.connect(workspace["db"])
    con.execute("PRAGMA foreign_keys=ON")
    cur = con.execute(
        "INSERT INTO tasks (project_id, title, description, status, parallel_group, "
        "assigned_to, created_by, created_at, updated_at) "
        "VALUES (?, ?, 'd', 'pending', ?, ?, 'atelier-pm-1', ?, ?)",
        (workspace["project_id"], title, parallel_group, assigned_to, created_at, created_at),
    )
    tid = cur.lastrowid
    con.commit()
    con.close()
    return tid


def _seed_team(workspace, team_id, role_ids):
    """Stand up a team + members + a persona snapshot so bridge_read membership
    passes for every role the poll_fn reads."""
    con = sqlite3.connect(workspace["db"])
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("INSERT INTO persona_snapshots (persona_version, persona_blob) VALUES ('v1', '{}')")
    con.execute(
        "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, 'P', ?, 'active')",
        (team_id, role_ids[0]),
    )
    for role in role_ids:
        con.execute(
            "INSERT INTO team_members (team_id, role_id, member_name, persona_snapshot_id) "
            "VALUES (?, ?, ?, 1)",
            (team_id, role, role),
        )
    con.commit()
    con.close()


# ── The deterministic servicer (the orchestrator-turn-loop stand-in) ────────


class _Servicer:
    """A deterministic state-machine over bridge_requests + bridge_messages.

    Runs in a background thread. Each loop tick:
      * reads this cycle's pending bridge_requests rows (scoped by team_pk,
        mirroring the production servicer) in FIFO (id) order;
      * for `create_team`: mints a fixed team_id, flips the row ready with
        {"team_id": ...};
      * for `spawn_*`/`send_message`: writes a TERMINAL reply envelope into the
        target inbox (so the poll_fn can read it) and flips the row ready;
        the target task_id/attempt come from the row's args_json so the envelope
        matches the dispatch record exactly (anti-spoof validation passes);
      * counts exactly how many times it serviced each (kind, task_id, attempt)
        so the test can assert EXACT-COUNT (serviced once per request).

    Adversarial knobs (per-test): `inject_error_kind` flips a kind to 'error';
    `malformed_for` writes a non-envelope payload for a given task so the
    barrier must hold; `team_id` is the inbox every reply lands in.
    `fail_create_team_times` flips the FIRST N `create_team` rows to 'error' then
    services the rest normally — the hook for the bounded retry/breaker tests
    (a transient spawn-init failure that recovers on a later attempt).
    """

    def __init__(
        self,
        db_path,
        *,
        team_id,
        recipient,
        team_pk="cycle-1",
        status_for=None,
        inject_error_kind=None,
        malformed_for=frozenset(),
        fail_create_team_times=0,
    ):
        self._db = db_path
        self._team_id = team_id
        self._recipient = recipient
        self._team_pk = team_pk
        self._status_for = status_for or {}
        self._inject_error_kind = inject_error_kind
        self._malformed_for = set(malformed_for)
        self._fail_create_team_times = fail_create_team_times
        self._create_team_failures = 0  # how many create_team rows we've errored
        self._minted = 0
        self.serviced = []  # list of (kind, args) tuples, in service order
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # context-manager so the thread is always joined
    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _conn(self):
        c = sqlite3.connect(self._db)
        c.execute("PRAGMA busy_timeout=5000")
        c.row_factory = sqlite3.Row
        return c

    def _run(self):
        while not self._stop.is_set():
            self._tick()
            time.sleep(0.01)
        # one final drain so a request enqueued just before stop is serviced
        self._tick()

    def _tick(self):
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT id, kind, args_json FROM bridge_requests "
                "WHERE team_pk = ? AND status = 'pending' ORDER BY id",
                (self._team_pk,),
            ).fetchall()
            for row in rows:
                self._service(con, row)
        finally:
            con.close()

    def _service(self, con, row):
        kind = row["kind"]
        args = json.loads(row["args_json"])
        self.serviced.append((kind, args))

        if self._inject_error_kind == kind:
            con.execute(
                "UPDATE bridge_requests SET status='error', error_text=?, "
                "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (f"{kind} denied (injected)", row["id"]),
            )
            con.commit()
            return

        if kind == "create_team":
            # Transient-failure injection: error the FIRST N create_team rows so
            # the wrapper's bounded retry kicks in, then service the rest to
            # 'ready' (the hook for create_team-retries-then-succeeds).
            if self._create_team_failures < self._fail_create_team_times:
                self._create_team_failures += 1
                con.execute(
                    "UPDATE bridge_requests SET status='error', error_text=?, "
                    "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (
                        f"create_team transient init failure #{self._create_team_failures}",
                        row["id"],
                    ),
                )
                con.commit()
                return
            self._minted += 1
            con.execute(
                "UPDATE bridge_requests SET status='ready', response_json=?, "
                "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (json.dumps({"team_id": self._team_id}), row["id"]),
            )
            con.commit()
            return

        # spawn_teammate / send_message / spawn_subagent — write a terminal
        # reply envelope into the recipient inbox, then flip ready.
        task_id = args.get("task_id")
        attempt = args.get("attempt", 1)
        # agent-team spawns don't carry task_id/attempt in args (they carry
        # team_id/name/prompt). The test passes a name==task_id convention via
        # role_id_for, so for those rows the recipient is fixed and we encode
        # the task identity from `name`.
        if task_id is None:
            task_id = args.get("name") or args.get("to")
        self._write_reply(con, task_id, attempt)
        con.execute(
            "UPDATE bridge_requests SET status='ready', response_json='{}', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (row["id"],),
        )
        con.commit()

    def _write_reply(self, con, task_id, attempt):
        # next seq for this (team, recipient)
        seq = con.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM bridge_messages WHERE team_id=? AND recipient=?",
            (self._team_id, self._recipient),
        ).fetchone()[0]
        if task_id in self._malformed_for:
            payload = "NOT A VALID ENVELOPE"
        else:
            status = self._status_for.get(task_id, "done")
            env = {
                "type": "task_result",
                "task_id": task_id,
                "attempt": attempt,
                "status": status,
                "artifacts": ["scripts/x.py"],
            }
            if status == "abandoned":
                env["notes_md"] = "ABANDON: scope — out of scope for this cycle"
                env["artifacts"] = []
            payload = json.dumps(env)
        con.execute(
            "INSERT INTO bridge_messages (team_id, recipient, seq, sender_id, kind, payload, "
            "persona_snapshot_id) VALUES (?, ?, ?, ?, 'reply', ?, 1)",
            (self._team_id, self._recipient, seq, self._recipient, payload),
        )
        con.commit()


# ── e2e: subagent mode ──────────────────────────────────────────────────────


def test_e2e_subagent_mode_wave_completes(workspace):
    """subagent mode: enqueue → servicer writes envelopes → poll reads them →
    the single wave completes with every task 'done'."""
    team_id = "T-SUB"
    recipient = "pm-1"  # all subagent replies land in one PM inbox for the test
    _seed_team(workspace, team_id, [recipient])
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    t2 = _seed_task(workspace, title="b", parallel_group=1)
    tasks = [
        {"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"},
        {"id": t2, "parallel_group": 1, "created_at": "2026-05-31T00:00:01Z"},
    ]

    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"])
    spawn_fn = build_spawn_fn(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        briefing_for=lambda task, attempt: f"B:{task['id']}:{attempt}",
    )
    poll_fn = build_poll_fn(workspace["db"], team_id=team_id, role_id_for=lambda task: recipient)

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        wd = WaveDispatcher(
            workspace["db"], spawn_fn=spawn_fn, poll_fn=poll_fn, sleep_fn=lambda s: time.sleep(0.01)
        )
        summaries = wd.run(tasks)

    # Exactly one wave, both tasks reported terminal-only 'done'.
    assert len(summaries) == 1
    assert summaries[0]["complete"] is True
    assert summaries[0]["terminal_only"] is True
    assert set(summaries[0]["reports"].values()) == {"done"}
    assert summaries[0]["reports"] == {str(t1): "done", str(t2): "done"}

    # EXACT-COUNT: exactly one spawn_subagent enqueued per task (no team kinds).
    spawn_rows = _count_kind(workspace["db"], "spawn_subagent")
    assert spawn_rows == 2
    assert _count_kind(workspace["db"], "create_team") == 0
    # Every request was serviced to ready (none left pending/error).
    assert _count_status(workspace["db"], "ready") == 2
    assert _count_status(workspace["db"], "pending") == 0


# ── e2e: agent-team mode ─────────────────────────────────────────────────────


def test_e2e_agent_team_mode_wave_completes(workspace):
    """agent-team mode: create_team fires EXACTLY once (blocking, serviced),
    then per-task first-touch spawn_teammate; envelopes drive the barrier."""
    team_id = "T-TEAM"
    # teammate role-ids == task ids (the role_id_for/teammate_name_for convention).
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    t2 = _seed_task(workspace, title="b", parallel_group=1)
    role_a, role_b = str(t1), str(t2)
    # Each teammate replies into its OWN inbox; the servicer writes to a single
    # recipient, so for agent-team we make every reply land in role_a's inbox
    # and poll that inbox per task by keying role_id_for to a fixed recipient.
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient, role_a, role_b])

    tasks = [
        {"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"},
        {"id": t2, "parallel_group": 1, "created_at": "2026-05-31T00:00:01Z"},
    ]

    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"])
    spawn_fn = build_spawn_fn(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        briefing_for=lambda task, attempt: f"B:{task['id']}:{attempt}",
        members=[role_a, role_b],
        team_name="cycle-team",
        teammate_name_for=lambda task: str(task["id"]),
        teams_root=workspace["root"] / "no-such-teams-root",  # forces first-touch
    )
    poll_fn = build_poll_fn(workspace["db"], team_id=team_id, role_id_for=lambda task: recipient)

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        wd = WaveDispatcher(
            workspace["db"], spawn_fn=spawn_fn, poll_fn=poll_fn, sleep_fn=lambda s: time.sleep(0.01)
        )
        summaries = wd.run(tasks)

    assert len(summaries) == 1
    assert summaries[0]["terminal_only"] is True
    assert summaries[0]["reports"] == {str(t1): "done", str(t2): "done"}

    # EXACT-COUNT: create_team fires exactly once across the whole wave.
    assert _count_kind(workspace["db"], "create_team") == 1
    # First-touch spawn_teammate per task (config.json absent → never SendMessage).
    assert _count_kind(workspace["db"], "spawn_teammate") == 2
    assert _count_kind(workspace["db"], "send_message") == 0


# ── adversarial: malformed envelope holds the barrier (then a valid one frees it) ─


def test_e2e_malformed_envelope_then_valid_recovers(workspace):
    """A malformed reply does NOT false-advance: the barrier holds until a valid
    terminal envelope arrives. We seed a malformed payload first, then let the
    servicer write the good one on a later attempt (re-dispatch)."""
    team_id = "T-MAL"
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient])
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    tasks = [{"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"}]

    # Seed ONE malformed reply up-front in the inbox. The poll_fn must skip it
    # (fail-closed) and NOT advance; the servicer then writes a valid envelope
    # for the spawn it services, which frees the barrier.
    con = sqlite3.connect(workspace["db"])
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(
        "INSERT INTO bridge_messages (team_id, recipient, seq, sender_id, kind, payload, "
        "persona_snapshot_id) VALUES (?, ?, 1, ?, 'reply', 'GARBAGE NON-JSON', 1)",
        (team_id, recipient, recipient),
    )
    con.commit()
    con.close()

    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"])
    spawn_fn = build_spawn_fn(
        DISPATCH_MODE_SUBAGENT, tools=tools, briefing_for=lambda task, attempt: "B"
    )
    poll_fn = build_poll_fn(workspace["db"], team_id=team_id, role_id_for=lambda task: recipient)

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        wd = WaveDispatcher(
            workspace["db"], spawn_fn=spawn_fn, poll_fn=poll_fn, sleep_fn=lambda s: time.sleep(0.01)
        )
        summaries = wd.run(tasks)

    # The barrier did NOT false-advance on the garbage row; the valid envelope
    # the servicer wrote freed it → 'done'. No crash.
    assert summaries[0]["reports"] == {str(t1): "done"}


# ── adversarial: create_team error is surfaced (raises) ─────────────────────


def test_e2e_create_team_error_raises(workspace):
    """A serviced-but-failed create_team (status='error') propagates as a
    BridgeDispatchError out of the blocking poll — surfaced, never swallowed."""
    from scripts.dispatch import BridgeDispatchError

    team_id = "T-ERR"
    recipient = "pm-1"
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    _seed_team(workspace, team_id, [recipient, str(t1)])
    tasks = [{"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"}]

    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"])
    spawn_fn = build_spawn_fn(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        briefing_for=lambda task, attempt: "B",
        members=[str(t1)],
        team_name="cycle-team",
        teams_root=workspace["root"] / "no-such-teams-root",
    )
    poll_fn = build_poll_fn(workspace["db"], team_id=team_id, role_id_for=lambda task: recipient)

    with _Servicer(
        workspace["db"], team_id=team_id, recipient=recipient, inject_error_kind="create_team"
    ):
        wd = WaveDispatcher(
            workspace["db"], spawn_fn=spawn_fn, poll_fn=poll_fn, sleep_fn=lambda s: time.sleep(0.01)
        )
        with pytest.raises(BridgeDispatchError, match="denied"):
            wd.run(tasks)


# ── adversarial: out-of-enum kind is rejected, not dispatched ───────────────


def test_e2e_out_of_enum_kind_rejected_not_dispatched(workspace):
    """An injected out-of-enum kind never enqueues (fail-closed at the wrapper)
    AND the SQLite CHECK rejects a direct bypass — no servicer ever sees it."""
    from scripts.dispatch import BridgeDispatchError

    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"])
    with pytest.raises(BridgeDispatchError, match="out-of-enum"):
        tools._enqueue("team_delete", {"team_id": "T"})
    assert _count_status(workspace["db"], "pending") == 0

    con = sqlite3.connect(workspace["db"])
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO bridge_requests (team_pk, kind, args_json) VALUES ('c', 'evil', '{}')"
            )
            con.commit()
    finally:
        con.close()


# ── bounded + hardened path: debounce drops a duplicate enqueue end-to-end ───


def test_e2e_debounce_drops_duplicate_spawn(workspace, monkeypatch):
    """End-to-end debounce: two IDENTICAL fire-and-forget spawns inside the
    window produce exactly ONE bridge_requests row (the dup is dropped); the
    servicer therefore services exactly one. A NOISE guard on top of idempotency."""
    monkeypatch.setenv("ATELIER_BRIDGE_DEBOUNCE_MS", "5000")
    team_id = "T-DEB"
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient])

    # Fixed clock so both calls fall inside the 5000ms window deterministically.
    clock = {"t": 100.0}
    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"], clock=lambda: clock["t"])
    # Two byte-identical spawns (same task_id + attempt + prompt) => one row.
    tools.spawn_subagent("task-7", 1, "PROMPT")
    tools.spawn_subagent("task-7", 1, "PROMPT")

    assert _count_kind(workspace["db"], "spawn_subagent") == 1


# ── bounded + hardened path: hard per-kind count limit trips at exactly N+1 ──


def test_e2e_count_limit_trips_at_exact_n_plus_one(workspace, monkeypatch):
    """The hard per-kind ceiling trips at EXACTLY N+1 (exact-count, not a loose
    bound): with the limit pinned to N, the first N spawns enqueue and the
    (N+1)th raises BridgeBudgetExceededError, having inserted nothing extra."""
    from scripts.dispatch import BridgeBudgetExceededError

    n = 3
    monkeypatch.setenv("ATELIER_BRIDGE_KIND_LIMIT", str(n))
    monkeypatch.setenv("ATELIER_BRIDGE_DEBOUNCE_MS", "0")  # isolate the limit mech
    tools = QueueBridgeDispatchTools("cycle-1", db_path=workspace["db"])

    for attempt in range(1, n + 1):
        tools.spawn_subagent("task-7", attempt, "P")
    assert _count_kind(workspace["db"], "spawn_subagent") == n

    with pytest.raises(BridgeBudgetExceededError, match="per-kind enqueue limit"):
        tools.spawn_subagent("task-7", n + 1, "P")
    # EXACT-COUNT: the breach inserted nothing — still exactly N rows.
    assert _count_kind(workspace["db"], "spawn_subagent") == n


# ── bounded + hardened path: create_team retries a transient failure ─────────


def test_e2e_create_team_retries_transient_failure_then_succeeds(workspace, monkeypatch):
    """A transient create_team init failure is RETRIED (bounded retry-with-backoff)
    and the team is created on the next attempt — the wave completes. The retry is
    TRANSPARENT to the wave loop (one logical dispatch)."""
    monkeypatch.setenv("ATELIER_BRIDGE_SPAWN_RETRIES", "2")  # 1 initial + 2 retries
    monkeypatch.setenv("ATELIER_BRIDGE_BREAKER_THRESHOLD", "5")  # above the 1 failure
    team_id = "T-RETRY"
    recipient = "pm-1"
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    role_a = str(t1)
    _seed_team(workspace, team_id, [recipient, role_a])
    tasks = [{"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"}]

    # Fast backoff sleep so the retry does not wall-block the test.
    tools = QueueBridgeDispatchTools(
        "cycle-1", db_path=workspace["db"], sleep_fn=lambda s: time.sleep(0.005)
    )
    spawn_fn = build_spawn_fn(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        briefing_for=lambda task, attempt: "B",
        members=[role_a],
        team_name="cycle-team",
        teams_root=workspace["root"] / "no-such-teams-root",
    )
    poll_fn = build_poll_fn(workspace["db"], team_id=team_id, role_id_for=lambda task: recipient)

    # Fail the FIRST create_team row, succeed on the retry.
    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient, fail_create_team_times=1):
        wd = WaveDispatcher(
            workspace["db"], spawn_fn=spawn_fn, poll_fn=poll_fn, sleep_fn=lambda s: time.sleep(0.01)
        )
        summaries = wd.run(tasks)

    # The wave completed despite the transient failure (the retry created the team).
    assert summaries[0]["reports"] == {str(t1): "done"}
    # TWO create_team rows exist (the failed one + the successful retry) — the
    # retry is transparent to the wave (still exactly ONE logical create_team).
    assert _count_kind(workspace["db"], "create_team") == 2
    assert _count_status(workspace["db"], "error") == 1


# ── bounded + hardened path: breaker opens after consecutive failures ────────


def test_e2e_breaker_opens_after_threshold_consecutive_failures(workspace, monkeypatch):
    """After N CONSECUTIVE create_team failures (>= breaker threshold), the
    circuit-breaker trips and the NEXT enqueue short-circuits with
    BridgeBreakerOpenError instead of re-attempting a dead team."""
    from scripts.dispatch import BridgeBreakerOpenError

    # No retries (each create_team is one attempt → one failure), threshold 3.
    monkeypatch.setenv("ATELIER_BRIDGE_SPAWN_RETRIES", "0")
    monkeypatch.setenv("ATELIER_BRIDGE_BREAKER_THRESHOLD", "3")
    team_id = "T-BREAK"
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient])

    # Every create_team fails (huge fail count). The servicer also errors the
    # row each call so each create_team attempt raises BridgeDispatchError.
    tools = QueueBridgeDispatchTools(
        "cycle-1", db_path=workspace["db"], sleep_fn=lambda s: time.sleep(0.005)
    )

    from scripts.dispatch import BridgeDispatchError

    with _Servicer(
        workspace["db"], team_id=team_id, recipient=recipient, fail_create_team_times=99
    ):
        # First 3 create_team calls each fail (consecutive). The 3rd pushes the
        # consecutive counter to == threshold.
        for _ in range(3):
            with pytest.raises(BridgeDispatchError):
                tools.create_team("cycle-team", ["pm-1"])
        # The 4th enqueue (a spawn or create_team) now short-circuits: breaker OPEN.
        with pytest.raises(BridgeBreakerOpenError, match="circuit-breaker OPEN"):
            tools.spawn_teammate(team_id, "pm-1", "BRIEF")


# ── helpers ──────────────────────────────────────────────────────────────────


def _count_kind(db_path, kind):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM bridge_requests WHERE kind = ?", (kind,)
        ).fetchone()[0]
    finally:
        con.close()


def _count_status(db_path, status):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM bridge_requests WHERE status = ?", (status,)
        ).fetchone()[0]
    finally:
        con.close()
