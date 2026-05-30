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
    DISPATCH_MODE_SUBAGENT,
    DispatchError,
    UnknownDispatchModeError,
    _team_member_names,
    build_spawn_fn,
    dispatch_task,
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


def test_resolve_dispatch_mode_defaults_to_subagent():
    assert resolve_dispatch_mode(env={}) == DISPATCH_MODE_SUBAGENT
    # Blank/whitespace is treated as unset.
    assert resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "   "}) == DISPATCH_MODE_SUBAGENT


def test_resolve_dispatch_mode_reads_env():
    assert (
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "agent-team"}) == DISPATCH_MODE_AGENT_TEAM
    )
    assert resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "subagent"}) == DISPATCH_MODE_SUBAGENT


def test_resolve_dispatch_mode_rejects_unknown():
    with pytest.raises(UnknownDispatchModeError):
        resolve_dispatch_mode(env={DISPATCH_MODE_ENV_VAR: "team"})


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
