"""pytest suite for the atelier#61 mode-specific dispatch seam in
``scripts/dispatch.py``.

#61 turns ``pm_dispatch.WaveDispatcher``'s mode-agnostic ``spawn_fn`` seam
(which carries zero mode knowledge — its docstring says "atelier#61 owns
spawning") into the concrete mode-specific tool action:

* sub-agent mode => one fire-and-forget ``Agent`` spawn per worker attempt;
* agent-team mode => ``TeamCreate`` once per cycle, then per-task FIRST-TOUCH
  ``Agent`` spawn / SUBSEQUENT ``SendMessage``.

``scripts/dispatch.py`` is pure Python and cannot call the Claude Code harness
tools directly, so all tool actions route through the injected
:class:`~scripts.dispatch.DispatchTools` Protocol — production binds a
queue-bridge wrapper (deferred), TESTS inject the ``_FakeTools`` recorder below.
This mirrors ``tests/test_pm_dispatch.py``'s style: inject fakes, record calls,
assert on the recorded sequence — no real subprocesses, no real harness tools.

Matrix covered here:

* sub-agent: ``build_spawn_fn("subagent")(task, attempt)`` records exactly ONE
  ``spawn_subagent`` with the right ``(task_id, attempt, briefing)`` and NOTHING
  team-related.
* agent-team FIRST-TOUCH (#59 gap): config.json WITHOUT the role's name =>
  ``spawn_teammate`` (an Agent spawn), NOT ``send_message``.
* agent-team SUBSEQUENT: config.json WITH the role's name => ``send_message``,
  NOT ``spawn_teammate``.
* missing config.json => first-touch (``spawn_teammate``).
* TeamCreate-once: across multiple ``spawn_fn`` calls, ``create_team`` fires
  exactly once.
* unknown mode => raises ``UnknownDispatchModeError`` (a ``DispatchError``).
* ``resolve_dispatch_mode``: env set vs. default vs. bad value.
* NON-VACUOUS: a test that would FAIL if first-touch collapsed to always-
  ``SendMessage`` (proves the Agent-spawn-on-first-touch is enforced).
"""

from __future__ import annotations

import json

import pytest

from scripts.dispatch import (
    DISPATCH_MODE_AGENT_TEAM,
    DISPATCH_MODE_ENV_VAR,
    DISPATCH_MODE_MARKER_RELPATH,
    DISPATCH_MODE_SUBAGENT,
    DispatchError,
    UnknownDispatchModeError,
    _team_member_names,
    build_spawn_fn,
    dispatch_task,
    persist_dispatch_mode,
    resolve_dispatch_mode,
)

# ── Fake injected tool boundary (records every call; no real harness) ───────


class _FakeTools:
    """Records every DispatchTools call in order. ``create_team`` returns a
    fixed team_id so the factory's once-per-cycle capture can be asserted."""

    def __init__(self, team_id: str = "team-xyz"):
        self._team_id = team_id
        self.calls: list[tuple] = []

    def create_team(self, name: str, members: list[str]) -> str:
        self.calls.append(("create_team", name, tuple(members)))
        return self._team_id

    def spawn_teammate(self, team_id: str, name: str, prompt: str) -> None:
        self.calls.append(("spawn_teammate", team_id, name, prompt))

    def send_message(self, team_id: str, to: str, message: str) -> None:
        self.calls.append(("send_message", team_id, to, message))

    def spawn_subagent(self, task_id, attempt: int, prompt: str) -> None:
        self.calls.append(("spawn_subagent", task_id, attempt, prompt))

    # convenience views
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _write_team_config(teams_root, team_id: str, member_names: list[str]) -> None:
    """Write a CC-shaped ``<teams_root>/<team_id>/config.json`` with the given
    ``members[].name`` list (mirrors the real CC team config shape kaizen #59
    inspects)."""
    cfg_dir = teams_root / team_id
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"members": [{"name": n} for n in member_names]}),
        encoding="utf-8",
    )


def _briefing_for(text: str = "BRIEFING"):
    """A trivial briefing source. Embeds the attempt so attempt-specific
    re-rendering is observable."""
    return lambda task, attempt: f"{text}:{task['id']}:{attempt}"


# ── resolve_dispatch_mode: env set vs default vs bad ────────────────────────


def test_resolve_dispatch_mode_defaults_to_subagent(tmp_path):
    # Empty env AND no marker under `root` => default. Pin `root` to an empty
    # tmp dir so the test is hermetic regardless of the repo's .ai/ state.
    assert resolve_dispatch_mode(env={}, root=tmp_path) == DISPATCH_MODE_SUBAGENT
    # Blank/whitespace is treated as unset.
    assert (
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "   "}, root=tmp_path)
        == DISPATCH_MODE_SUBAGENT
    )


def test_resolve_dispatch_mode_reads_env(tmp_path):
    assert (
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "agent-team"}, root=tmp_path)
        == DISPATCH_MODE_AGENT_TEAM
    )
    assert (
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "subagent"}, root=tmp_path)
        == DISPATCH_MODE_SUBAGENT
    )


def test_resolve_dispatch_mode_rejects_unknown(tmp_path):
    with pytest.raises(UnknownDispatchModeError):
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "team"}, root=tmp_path)


# ── mode persistence: persist_dispatch_mode + resolve precedence (atelier#62) ─


def test_persist_dispatch_mode_writes_marker(tmp_path):
    """persist_dispatch_mode writes a single-line marker under .ai/atelier.mode,
    creating .ai/ if absent."""
    persist_dispatch_mode("agent-team", root=tmp_path)
    marker = tmp_path / DISPATCH_MODE_MARKER_RELPATH
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == "agent-team"


def test_persist_dispatch_mode_rejects_invalid_without_writing(tmp_path):
    """An invalid mode raises UnknownDispatchModeError and leaves NO marker —
    a typo never persists a wedged state."""
    with pytest.raises(UnknownDispatchModeError):
        persist_dispatch_mode("teammode", root=tmp_path)
    assert not (tmp_path / DISPATCH_MODE_MARKER_RELPATH).exists()


def test_resolve_marker_beats_default(tmp_path):
    """With no env var, the persisted marker wins over the subagent default."""
    persist_dispatch_mode("agent-team", root=tmp_path)
    assert resolve_dispatch_mode(env={}, root=tmp_path) == DISPATCH_MODE_AGENT_TEAM


def test_resolve_env_override_beats_marker(tmp_path):
    """The env override beats the persisted marker (back-compat / smoke runs).

    NON-VACUOUS: the marker says agent-team; if env did NOT win, this would
    return agent-team and the assertion would fail."""
    persist_dispatch_mode("agent-team", root=tmp_path)
    assert (
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "subagent"}, root=tmp_path)
        == DISPATCH_MODE_SUBAGENT
    )


def test_resolve_default_when_neither_env_nor_marker(tmp_path):
    """No env, no marker => subagent default."""
    assert not (tmp_path / DISPATCH_MODE_MARKER_RELPATH).exists()
    assert resolve_dispatch_mode(env={}, root=tmp_path) == DISPATCH_MODE_SUBAGENT


def test_resolve_rejects_corrupt_marker_value(tmp_path):
    """A marker file with a non-canonical value fails loud (not silent default)."""
    marker = tmp_path / DISPATCH_MODE_MARKER_RELPATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("nonsense\n", encoding="utf-8")
    with pytest.raises(UnknownDispatchModeError):
        resolve_dispatch_mode(env={}, root=tmp_path)


def test_persist_then_resolve_round_trip_both_modes(tmp_path):
    """Round-trip: persist each canonical mode and read it back via resolve."""
    for mode in (DISPATCH_MODE_SUBAGENT, DISPATCH_MODE_AGENT_TEAM):
        persist_dispatch_mode(mode, root=tmp_path)
        assert resolve_dispatch_mode(env={}, root=tmp_path) == mode


# ── _team_member_names: first-touch detection helper ────────────────────────


def test_team_member_names_reads_config(tmp_path):
    _write_team_config(tmp_path, "t1", ["pm-1", "sdet-1"])
    assert _team_member_names("t1", tmp_path) == {"pm-1", "sdet-1"}


def test_team_member_names_missing_config_is_empty(tmp_path):
    # Missing config.json => treated as "no members yet" (first-touch), NOT error.
    assert _team_member_names("nope", tmp_path) == set()


def test_team_member_names_malformed_json_is_empty(tmp_path):
    cfg_dir = tmp_path / "t1"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not json", encoding="utf-8")
    assert _team_member_names("t1", tmp_path) == set()


def test_team_member_names_tolerates_bad_member_shapes(tmp_path):
    cfg_dir = tmp_path / "t1"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"members": [{"name": "pm-1"}, {"noname": 1}, "junk", {"name": 7}]}),
        encoding="utf-8",
    )
    # Only the well-formed string-name entry survives.
    assert _team_member_names("t1", tmp_path) == {"pm-1"}


# ── sub-agent path: spawn_subagent fire-and-forget only ─────────────────────


def test_subagent_build_spawn_fn_records_one_spawn_subagent(tmp_path):
    """sub-agent mode: a single spawn_fn call => exactly one spawn_subagent with
    the correct (task_id, attempt, briefing) and NO team-related calls."""
    tools = _FakeTools()
    spawn = build_spawn_fn(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        briefing_for=_briefing_for(),
        teams_root=tmp_path,
    )
    spawn({"id": 42}, 3)

    assert tools.calls == [("spawn_subagent", 42, 3, "BRIEFING:42:3")]
    # NOTHING team-related.
    assert "create_team" not in tools.names()
    assert "spawn_teammate" not in tools.names()
    assert "send_message" not in tools.names()


def test_subagent_dispatch_task_ignores_team_args(tmp_path):
    """dispatch_task in sub-agent mode ignores team_id/teammate_name entirely."""
    tools = _FakeTools()
    dispatch_task(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        task={"id": "t9"},
        attempt=1,
        briefing="B",
        team_id="ignored",
        teammate_name="ignored",
        teams_root=tmp_path,
    )
    assert tools.calls == [("spawn_subagent", "t9", 1, "B")]


# ── agent-team FIRST-TOUCH (#59 gap test) ───────────────────────────────────


def test_agent_team_first_touch_spawns_teammate_not_send_message(tmp_path):
    """config.json WITHOUT the role's name => first-touch => spawn_teammate
    (an Agent spawn), NOT a naked send_message. THIS is the #59 gap test."""
    tools = _FakeTools(team_id="T")
    # Team exists but the role is NOT yet a member.
    _write_team_config(tmp_path, "T", ["pm-1"])

    dispatch_task(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        task={"id": "t1"},
        attempt=1,
        briefing="FULL-BRIEFING",
        team_id="T",
        teammate_name="sdet-1",  # absent from members => first-touch
        teams_root=tmp_path,
    )

    # It was an Agent spawn carrying the full briefing …
    assert tools.calls == [("spawn_teammate", "T", "sdet-1", "FULL-BRIEFING")]
    # … and CRUCIALLY not a SendMessage (the #59 inbox-drop bug).
    assert "send_message" not in tools.names()


def test_agent_team_missing_config_is_first_touch(tmp_path):
    """A MISSING config.json => treated as first-touch => spawn_teammate."""
    tools = _FakeTools(team_id="T")
    # No config written at all.
    dispatch_task(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        task={"id": "t1"},
        attempt=1,
        briefing="B",
        team_id="T",
        teammate_name="sdet-1",
        teams_root=tmp_path,
    )
    assert tools.names() == ["spawn_teammate"]


# ── agent-team SUBSEQUENT ───────────────────────────────────────────────────


def test_agent_team_subsequent_uses_send_message_not_spawn(tmp_path):
    """config.json WITH the role's name => already spawned => send_message,
    NOT spawn_teammate."""
    tools = _FakeTools(team_id="T")
    _write_team_config(tmp_path, "T", ["pm-1", "sdet-1"])

    dispatch_task(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        task={"id": "t1"},
        attempt=2,
        briefing="MSG",
        team_id="T",
        teammate_name="sdet-1",  # already a member => subsequent
        teams_root=tmp_path,
    )

    assert tools.calls == [("send_message", "T", "sdet-1", "MSG")]
    assert "spawn_teammate" not in tools.names()


def test_agent_team_first_then_subsequent_transition(tmp_path):
    """The realistic lifecycle: first dispatch (no member) spawns; after CC
    writes the member into config.json, the next dispatch sends a message.

    NON-VACUOUS: if dispatch collapsed first-touch to always-send_message, the
    FIRST call would be a send_message and this assertion on call[0] would
    fail."""
    tools = _FakeTools(team_id="T")
    # Round 1: team has no sdet-1 yet.
    _write_team_config(tmp_path, "T", ["pm-1"])
    dispatch_task(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        task={"id": "t1"},
        attempt=1,
        briefing="B1",
        team_id="T",
        teammate_name="sdet-1",
        teams_root=tmp_path,
    )
    # CC materialises the teammate => it now appears in members.
    _write_team_config(tmp_path, "T", ["pm-1", "sdet-1"])
    dispatch_task(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        task={"id": "t1"},
        attempt=2,
        briefing="B2",
        team_id="T",
        teammate_name="sdet-1",
        teams_root=tmp_path,
    )

    assert tools.calls == [
        ("spawn_teammate", "T", "sdet-1", "B1"),  # first-touch is an Agent spawn
        ("send_message", "T", "sdet-1", "B2"),  # subsequent is a SendMessage
    ]


# ── agent-team required-arg guards ──────────────────────────────────────────


def test_agent_team_dispatch_requires_team_id_and_name(tmp_path):
    tools = _FakeTools()
    with pytest.raises(DispatchError):
        dispatch_task(
            DISPATCH_MODE_AGENT_TEAM,
            tools=tools,
            task={"id": "t1"},
            attempt=1,
            briefing="B",
            team_id=None,
            teammate_name="sdet-1",
            teams_root=tmp_path,
        )
    with pytest.raises(DispatchError):
        dispatch_task(
            DISPATCH_MODE_AGENT_TEAM,
            tools=tools,
            task={"id": "t1"},
            attempt=1,
            briefing="B",
            team_id="T",
            teammate_name=None,
            teams_root=tmp_path,
        )
    assert tools.calls == []  # nothing dispatched on a guard failure


# ── TeamCreate-once across multiple spawn_fn calls ──────────────────────────


def test_build_spawn_fn_team_create_called_exactly_once(tmp_path):
    """Across MANY spawn_fn calls in agent-team mode, create_team fires exactly
    once per cycle; every dispatch reuses the captured team_id."""
    tools = _FakeTools(team_id="T-CAPTURED")
    # No member is ever written, so every dispatch is a (first-touch) spawn —
    # which keeps the assertion focused on the create_team count.
    spawn = build_spawn_fn(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        briefing_for=_briefing_for(),
        members=["pm-1", "sdet-1", "be-1"],
        team_name="cycle-team",
        teammate_name_for=lambda task: str(task["id"]),
        teams_root=tmp_path,
    )

    for i in range(4):
        spawn({"id": f"role-{i}"}, 1)

    create_calls = [c for c in tools.calls if c[0] == "create_team"]
    assert len(create_calls) == 1
    assert create_calls[0] == ("create_team", "cycle-team", ("pm-1", "sdet-1", "be-1"))
    # Every spawn reused the SAME captured team_id.
    spawn_calls = [c for c in tools.calls if c[0] == "spawn_teammate"]
    assert len(spawn_calls) == 4
    assert all(c[1] == "T-CAPTURED" for c in spawn_calls)


def test_build_spawn_fn_subagent_never_creates_team(tmp_path):
    """sub-agent mode never touches create_team even across many spawns."""
    tools = _FakeTools()
    spawn = build_spawn_fn(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        briefing_for=_briefing_for(),
        teams_root=tmp_path,
    )
    for i in range(3):
        spawn({"id": i}, 1)
    assert tools.names() == ["spawn_subagent", "spawn_subagent", "spawn_subagent"]
    assert "create_team" not in tools.names()


def test_build_spawn_fn_matches_wavedispatcher_seam(tmp_path):
    """The returned spawn_fn has the WaveDispatcher seam shape
    ``spawn_fn(task, attempt) -> None`` (the whole point: a later production
    issue can drop it into WaveDispatcher unchanged)."""
    tools = _FakeTools()
    spawn = build_spawn_fn(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        briefing_for=_briefing_for(),
        teams_root=tmp_path,
    )
    # Positional (task, attempt) call, returns None.
    result = spawn({"id": "x"}, 5)
    assert result is None
    assert tools.calls == [("spawn_subagent", "x", 5, "BRIEFING:x:5")]


# ── unknown mode raises ─────────────────────────────────────────────────────


def test_dispatch_task_unknown_mode_raises(tmp_path):
    tools = _FakeTools()
    with pytest.raises(UnknownDispatchModeError) as exc:
        dispatch_task(
            "team",  # not a canonical value
            tools=tools,
            task={"id": "t1"},
            attempt=1,
            briefing="B",
            teams_root=tmp_path,
        )
    # It is a DispatchError subclass (operator-facing fail-loud).
    assert isinstance(exc.value, DispatchError)
    assert tools.calls == []


def test_build_spawn_fn_unknown_mode_raises_at_build_time(tmp_path):
    """A bad mode fails at factory-build time, not on first dispatch."""
    with pytest.raises(UnknownDispatchModeError):
        build_spawn_fn(
            "nonsense",
            tools=_FakeTools(),
            briefing_for=_briefing_for(),
            teams_root=tmp_path,
        )


def test_build_spawn_fn_agent_team_requires_team_name(tmp_path):
    with pytest.raises(DispatchError):
        build_spawn_fn(
            DISPATCH_MODE_AGENT_TEAM,
            tools=_FakeTools(),
            briefing_for=_briefing_for(),
            members=["pm-1"],
            team_name=None,
            teams_root=tmp_path,
        )


# ── atelier#81: production queue-bridge transport (QueueBridgeDispatchTools) ──
#
# These exercise the LIVE binding of the DispatchTools Protocol against the real
# bridge_requests table (migrations/shared/008) — no mocks of the queue itself
# (the orchestrator servicer is the only thing faked, via direct row UPDATEs).

import sqlite3  # noqa: E402 — co-located with the production-wrapper tests below
from pathlib import Path  # noqa: E402

from scripts.dispatch import (  # noqa: E402
    BRIDGE_REQUEST_KINDS,
    BridgeDispatchError,
    QueueBridgeDispatchTools,
    build_poll_fn,
)
from scripts.migrate import apply_migrations  # noqa: E402

_MIGRATIONS_SHARED = Path(__file__).resolve().parent.parent / "migrations" / "shared"


@pytest.fixture
def bridge_db(tmp_path):
    """A real Local DB with shared/ migrations applied — carries both the
    bridge_requests request-queue (008) and the bridge_messages wire (003)."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), _MIGRATIONS_SHARED)
    return str(db)


def _rows(db_path, **where):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        clause = " AND ".join(f"{k} = ?" for k in where)
        sql = "SELECT * FROM bridge_requests"
        if clause:
            sql += f" WHERE {clause}"
        sql += " ORDER BY id"
        return [dict(r) for r in con.execute(sql, tuple(where.values())).fetchall()]
    finally:
        con.close()


def _service_row(db_path, row_id, *, status, response=None, error=None):
    """Stand in for the orchestrator servicer: flip a pending row to ready/error."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE bridge_requests SET status = ?, response_json = ?, error_text = ?, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (status, json.dumps(response) if response is not None else None, error, row_id),
        )
        con.commit()
    finally:
        con.close()


def test_kind_enum_string_identical_to_protocol_method_names():
    """The 008 kind enum MUST be string-identical to the DispatchTools method
    names so the servicer maps kind->method by name with zero translation."""
    expected = {"create_team", "spawn_teammate", "send_message", "spawn_subagent"}
    assert expected == BRIDGE_REQUEST_KINDS


def test_fire_and_forget_methods_enqueue_and_return_none(bridge_db):
    """spawn_teammate / send_message / spawn_subagent each enqueue exactly ONE
    pending row and return None WITHOUT polling (fire-and-forget)."""
    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db)

    assert tools.spawn_teammate("T", "sdet-1", "BRIEF") is None
    assert tools.send_message("T", "be-1", "MSG") is None
    assert tools.spawn_subagent("task-7", 2, "PROMPT") is None

    rows = _rows(bridge_db)
    assert [r["kind"] for r in rows] == ["spawn_teammate", "send_message", "spawn_subagent"]
    # Every fire-and-forget row is DURABLE + still pending (never serviced here).
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["team_pk"] == "cycle-1" for r in rows)
    # args_json round-trips the tool args as DATA.
    assert json.loads(rows[0]["args_json"]) == {"team_id": "T", "name": "sdet-1", "prompt": "BRIEF"}
    assert json.loads(rows[2]["args_json"]) == {
        "task_id": "task-7",
        "attempt": 2,
        "prompt": "PROMPT",
    }


def test_create_team_blocks_until_ready_then_returns_team_id(bridge_db):
    """create_team enqueues, polls its OWN row, and returns the serviced
    team_id once the row flips to 'ready'. (Servicer flips it after enqueue.)"""
    ticks = {"n": 0}

    def fake_clock():
        return float(ticks["n"])

    def fake_sleep(_seconds):
        # Simulate the orchestrator servicing the row on the 2nd poll.
        ticks["n"] += 1
        if ticks["n"] == 2:
            pending = _rows(bridge_db, kind="create_team", status="pending")
            _service_row(bridge_db, pending[0]["id"], status="ready", response={"team_id": "T-99"})

    tools = QueueBridgeDispatchTools(
        "cycle-1", db_path=bridge_db, clock=fake_clock, sleep_fn=fake_sleep
    )
    team_id = tools.create_team("cycle-team", ["pm-1", "sdet-1"])
    assert team_id == "T-99"
    # The row was enqueued with the right kind + args, then serviced to ready.
    row = _rows(bridge_db, kind="create_team")[0]
    assert row["status"] == "ready"
    assert json.loads(row["args_json"]) == {"name": "cycle-team", "members": ["pm-1", "sdet-1"]}


def test_create_team_raises_on_error_status(bridge_db):
    """A serviced-but-FAILED row (status='error') makes create_team RAISE —
    the 3-state status is exactly why 'error' exists (no infinite spin)."""

    def fake_sleep(_seconds):
        pending = _rows(bridge_db, kind="create_team", status="pending")
        if pending:
            _service_row(bridge_db, pending[0]["id"], status="error", error="TeamCreate denied")

    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db, sleep_fn=fake_sleep)
    with pytest.raises(BridgeDispatchError, match="TeamCreate denied"):
        tools.create_team("cycle-team", ["pm-1"])


def test_create_team_times_out_and_raises_never_spins(bridge_db):
    """If the orchestrator never services the row, create_team RAISES at the
    bounded PER_CALL_TIMEOUT_S — never an unbounded spin."""
    ticks = {"n": 0}

    def fake_clock():
        # Jump straight past the budget on the second reading.
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else QueueBridgeDispatchTools.PER_CALL_TIMEOUT_S + 1.0

    tools = QueueBridgeDispatchTools(
        "cycle-1", db_path=bridge_db, clock=fake_clock, sleep_fn=lambda _s: None
    )
    with pytest.raises(BridgeDispatchError, match="timed out"):
        tools.create_team("cycle-team", ["pm-1"])
    # The row is still pending (never serviced) — durable, not silently dropped.
    assert _rows(bridge_db, kind="create_team")[0]["status"] == "pending"


def test_create_team_raises_on_ready_without_team_id(bridge_db):
    """A 'ready' row whose response_json lacks a team_id string is a contract
    violation → raise (never return a bogus team_id)."""

    def fake_sleep(_seconds):
        pending = _rows(bridge_db, kind="create_team", status="pending")
        if pending:
            _service_row(bridge_db, pending[0]["id"], status="ready", response={"wrong": "key"})

    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db, sleep_fn=fake_sleep)
    with pytest.raises(BridgeDispatchError, match="team_id"):
        tools.create_team("cycle-team", ["pm-1"])


def test_enqueue_rejects_out_of_enum_kind(bridge_db):
    """Fail-closed: an out-of-enum kind is rejected at enqueue BEFORE it can
    reach the SQLite CHECK (clear BridgeDispatchError, not an IntegrityError)."""
    tools = QueueBridgeDispatchTools("cycle-1", db_path=bridge_db)
    with pytest.raises(BridgeDispatchError, match="out-of-enum"):
        tools._enqueue("team_delete", {"team_id": "T"})  # not a DispatchTools method
    # Nothing was enqueued.
    assert _rows(bridge_db) == []


def test_sqlite_check_rejects_out_of_enum_kind(bridge_db):
    """Defense in depth: even a direct INSERT bypassing the wrapper is rejected
    by the 008 CHECK constraint (the closed enum is enforced at the DB too)."""
    con = sqlite3.connect(bridge_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO bridge_requests (team_pk, kind, args_json) VALUES (?, ?, ?)",
                ("cycle-1", "rm_rf_slash", "{}"),
            )
            con.commit()
    finally:
        con.close()


# ── poll_fn: terminal-reply-envelope read from bridge_messages ──────────────


def _seed_team_with_member(db_path, team_id, role_id):
    """Stand up a team + member + persona snapshot so bridge_read membership
    passes and a reply row can be seeded."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            "INSERT INTO persona_snapshots (persona_version, persona_blob) VALUES ('v1', '{}')"
        )
        con.execute(
            "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, 'P', ?, 'active')",
            (team_id, role_id),
        )
        con.execute(
            "INSERT INTO team_members (team_id, role_id, member_name, persona_snapshot_id) "
            "VALUES (?, ?, ?, 1)",
            (team_id, role_id, role_id),
        )
        con.commit()
    finally:
        con.close()


def _seed_reply(db_path, team_id, recipient, seq, sender, payload):
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            "INSERT INTO bridge_messages (team_id, recipient, seq, sender_id, kind, payload, "
            "persona_snapshot_id) VALUES (?, ?, ?, ?, 'reply', ?, 1)",
            (team_id, recipient, seq, sender, payload),
        )
        con.commit()
    finally:
        con.close()


def _terminal_envelope(task_id, attempt, status="done"):
    return json.dumps(
        {
            "type": "task_result",
            "task_id": task_id,
            "attempt": attempt,
            "status": status,
            "artifacts": ["scripts/foo.py"],
        }
    )


def test_poll_fn_returns_validated_terminal_envelope(bridge_db):
    """poll_fn reads the worker's terminal reply from bridge_messages, validates
    it fail-closed, and returns the parsed Mapping."""
    _seed_team_with_member(bridge_db, "T", "pm-1")
    # The reply lands in the PM's inbox (recipient='pm-1') from the worker.
    _seed_reply(bridge_db, "T", "pm-1", 1, "pm-1", _terminal_envelope("task-1", 1, "done"))

    poll = build_poll_fn(bridge_db, team_id="T", role_id_for=lambda task: "pm-1")
    result = poll({"id": "task-1"}, 1)
    assert result is not None
    assert result["status"] == "done"
    assert result["task_id"] == "task-1"


def test_poll_fn_returns_none_when_no_reply(bridge_db):
    """No reply row yet => None (NOT {}), so the wave barrier HOLDS."""
    _seed_team_with_member(bridge_db, "T", "pm-1")
    poll = build_poll_fn(bridge_db, team_id="T", role_id_for=lambda task: "pm-1")
    assert poll({"id": "task-1"}, 1) is None


def test_poll_fn_holds_barrier_on_non_terminal_status(bridge_db):
    """A VALID but non-terminal (blocked / needs-input) reply => None (the
    barrier holds; blocked/needs-input also emit replies per 006)."""
    _seed_team_with_member(bridge_db, "T", "pm-1")
    blocked = json.dumps(
        {
            "type": "task_result",
            "task_id": "task-1",
            "attempt": 1,
            "status": "blocked",
            "artifacts": [],
        }
    )
    _seed_reply(bridge_db, "T", "pm-1", 1, "pm-1", blocked)
    poll = build_poll_fn(bridge_db, team_id="T", role_id_for=lambda task: "pm-1")
    assert poll({"id": "task-1"}, 1) is None


def test_poll_fn_fail_closed_on_malformed_envelope(bridge_db):
    """A malformed / non-JSON reply => None (fail-closed: never a false advance,
    never a crash)."""
    _seed_team_with_member(bridge_db, "T", "pm-1")
    _seed_reply(bridge_db, "T", "pm-1", 1, "pm-1", "this is not a JSON envelope at all")
    poll = build_poll_fn(bridge_db, team_id="T", role_id_for=lambda task: "pm-1")
    assert poll({"id": "task-1"}, 1) is None


def test_poll_fn_rejects_cross_task_and_attempt_spoof(bridge_db):
    """A terminal envelope whose task_id/attempt mismatch the dispatch record is
    rejected by validate_envelope => None (anti cross-task spoof)."""
    _seed_team_with_member(bridge_db, "T", "pm-1")
    # Envelope claims task-OTHER / attempt 9, but we dispatched task-1 / attempt 1.
    _seed_reply(bridge_db, "T", "pm-1", 1, "pm-1", _terminal_envelope("task-OTHER", 9, "done"))
    poll = build_poll_fn(bridge_db, team_id="T", role_id_for=lambda task: "pm-1")
    assert poll({"id": "task-1"}, 1) is None


def test_poll_fn_returns_none_on_read_error(bridge_db):
    """If the team does not exist yet (read raises), poll_fn HOLDS the barrier
    (returns None) rather than crashing or false-advancing."""
    # No team seeded => bridge_read raises ChannelMissingError internally.
    poll = build_poll_fn(bridge_db, team_id="GHOST", role_id_for=lambda task: "pm-1")
    assert poll({"id": "task-1"}, 1) is None


# ── fence round-trip: _parse_reply_envelope reverses bridge_read._fence ──────


def test_parse_reply_envelope_round_trips_real_fence():
    """The load-bearing unescape: a payload fenced by the REAL
    ``bridge_read._fence`` (HTML-escaped element content) must round-trip back
    to the worker's JSON envelope — including HTML-special chars (< > &), proving
    the &amp;-last unescape ordering is correct (a wrong order corrupts &amp;lt;)."""
    from scripts.bridge_read import _fence
    from scripts.dispatch import _parse_reply_envelope

    env = {
        "type": "task_result",
        "task_id": "t1",
        "attempt": 1,
        "status": "done",
        "artifacts": ["a<b>&c", "d&amp;e"],
    }
    fenced = _fence(json.dumps(env), "pm-1", 7)
    assert _parse_reply_envelope(fenced) == env
    # Defensive: a raw (un-fenced) JSON object also parses.
    assert _parse_reply_envelope(json.dumps(env)) == env


def test_parse_reply_envelope_returns_none_on_non_object():
    """Non-object / non-JSON / non-str payloads => None (never raise), so the
    poll keeps scanning rather than crash or false-advance."""
    from scripts.dispatch import _parse_reply_envelope

    assert _parse_reply_envelope("not json at all") is None
    assert _parse_reply_envelope("[1, 2, 3]") is None  # JSON array, not an object
    assert _parse_reply_envelope(None) is None
    assert _parse_reply_envelope(b"bytes") is None


# ═══════════════════════════════════════════════════════════════════════════
# atelier#66 [S3] AUDIT-NOTES — both-mode parity coverage map for #57-#65
# ═══════════════════════════════════════════════════════════════════════════
#
# #66 (epic #39 closer, AC5/AC6/AC7) requires every mode-dispatched DURABLE
# write added by #57-#65 to be exercised in BOTH Local and Memex mode. The audit
# surfaced COVERAGE gaps (not real bugs — no durable write is hard-wired to the
# wrong mode), each now closed by ONE focused force-Memex test using the
# canonical hermetic stub set (detect_mode->'memex' + backend._backend +
# backend._backend_is_memex, spying the backend_memex LEAF; conftest autouse
# _clear_mode_cache + _stub_singleton_workspace neutralize the registry):
#
#   T1  scripts/dispatch.py::_resolve_local_bridge_db   (this file, below)
#   T2  scripts/backend.py::write_task team_pk fold      (test_backend_dispatch.py)
#   T3  scripts/planner.py::persist_tasks parallel_group (test_planner.py)
#   T4  scripts/tasks.py status transitions (#60)        (test_pm_dispatch.py)
#   T5  scripts/documents.py::write_spec_amendment (#62) (test_spec_versioning.py)
#   T6  scripts/side_query.py + scripts/roster_extension (test_side_query.py /
#                                                          test_roster_extension.py)
#
# DELIBERATE EXCLUSIONS — these are CORRECTLY NOT both-mode-parity targets; the
# audit deliverable is documenting WHY, so a future maintainer does not "fix"
# them by adding a Memex route (which would itself be the §17 bug):
#
#   • scripts/dag.py — PURE (no I/O: no backend/sqlite/file imports). Mode is
#     irrelevant; it operates on in-memory task-graph dicts. No write to route.
#
#   • scripts/team_meeting.py — bridge_send (the always-Local message wire) +
#     backend.write_team_audit ONLY (team_meeting.py:458/478). team_audit_log is
#     ALWAYS-LOCAL by §17 (backend.write_team_audit binds backend_local directly,
#     mode-agnostic — backend.py:669-699), and bridge_send rides the same Local
#     wire. A Memex route here would FAIL (Memex has no team-mode tables) — so
#     "always-Local" IS the correct posture; parametrizing it over Memex would
#     assert the bug, not the contract.
#
#   • scripts/status.py — explicitly Local-ONLY by gate (status.py:554:
#     detect_mode() != "local" -> prints a notice + returns 0; covered by
#     test_status.py). It renders the migration-006 PM dispatch-state columns,
#     which are Local-only (the same reason tasks._dispatch_state_memex_guard
#     raises in Memex mode — see T4 in test_pm_dispatch.py). No Memex analog.
#
#   • scripts/tasks.py PM dispatch-state mutators (set_abandoned /
#     increment_attempt / stamp_last_attempt / set_abandoned_ack) — DELIBERATELY
#     Local-only for now (NotImplementedError guard in Memex mode); a documented
#     followup, NOT a #66 parity target. Pinned by T4's guard test so a future
#     Memex-parity landing surfaces as a RED reminder.
#
# bridge_db (T1) and team_audit_log are mode-AGNOSTIC by being HARD-WIRED Local;
# T1's value is asserting that hard-wiring holds (the anti-revert), NOT
# parametrizing it over Memex.
# ═══════════════════════════════════════════════════════════════════════════


# ── atelier#66 [S3] T1 — §17 bridge_db-is-always-Local pin (AC6) ─────────────
#
# `_resolve_local_bridge_db` resolves the request-queue DB to `.ai/atelier.db`
# under the CWD git root and MUST NEVER consult `mode_detector`: the team-mode
# bridge queue is Local-only by §17 (the Memex backend has no team-mode tables,
# so a mode-dispatched route would be the bug). These two tests are the
# ANTI-REVERT pin for that invariant — (a) proves the resolver ignores Memex
# mode, (b) proves it fails loud outside a git workspace. Routing the resolver
# through `detect_mode` (the §17 violation) makes (a) RED; the non-vacuity
# proof in the review log neuters exactly that path.


def test_resolve_local_bridge_db_ignores_mode(tmp_path, monkeypatch):
    """§17/AC6: `_resolve_local_bridge_db` returns `<git-root>/.ai/atelier.db`
    even when `detect_mode() == "memex"` — the bridge queue is hard-wired Local
    and the resolver MUST NOT branch on the durable mode. Anti-revert: a route
    through `mode_detector` (returning any Memex path in Memex mode) makes this
    RED."""
    from scripts import mode_detector
    from scripts.dispatch import _resolve_local_bridge_db

    # Force the durable mode to Memex — the resolver must ignore it entirely.
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "memex")

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(root)

    resolved = _resolve_local_bridge_db()
    # Always the Local .ai/atelier.db under the git root — never a Memex path.
    assert resolved == str(root.resolve() / ".ai" / "atelier.db")
    # The parent dir is created as a side effect (queue-ready).
    assert (root / ".ai").is_dir()


def test_resolve_local_bridge_db_raises_outside_git(tmp_path, monkeypatch):
    """Outside any git workspace the resolver fails loud with a
    `BridgeDispatchError` (operator-facing) rather than silently writing the
    queue to a stray CWD — the production transport requires CWD under the
    atelier workspace."""
    from scripts import mode_detector
    from scripts.dispatch import _resolve_local_bridge_db

    # Mode is irrelevant to the git-root requirement; pin it Local to show the
    # raise is about the missing workspace, not the mode.
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")

    bare = tmp_path / "not-a-repo"
    bare.mkdir()  # no .git anywhere up the tree (pytest tmp is outside the repo)
    monkeypatch.chdir(bare)

    with pytest.raises(BridgeDispatchError, match="not inside a git workspace"):
        _resolve_local_bridge_db()
