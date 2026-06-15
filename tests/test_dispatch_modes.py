"""pytest suite for the atelier#61 mode-specific dispatch seam in
``scripts/dispatch.py``.

#61 turns the mode-agnostic dispatch decision into the concrete mode-specific
tool action:

* sub-agent mode => one fire-and-forget ``Agent`` spawn per worker attempt;
* agent-team mode => per-task FIRST-TOUCH ``Agent`` spawn / SUBSEQUENT
  ``SendMessage``.

``scripts/dispatch.py`` is pure Python and cannot call the Claude Code harness
tools directly, so all tool actions route through the injected
:class:`~scripts.dispatch.DispatchTools` Protocol — production binds the
deterministic host's ``CliDispatchTools``, TESTS inject the ``_FakeTools``
recorder below. This mirrors ``tests/test_pm_dispatch.py``'s style: inject fakes,
record calls, assert on the recorded sequence — no real subprocesses, no real
harness tools.

NOTE (M7): the legacy production dispatch-queue transport (its queue-bridge
dispatch wrapper, the WaveDispatcher seam factories, the request-queue table, and
its ``BRIDGE_*`` tunables) was REMOVED. The
tests that exercised it were deleted; what remains here covers the kept
mode-seam surface — ``dispatch_task`` (the mode-branching dispatcher),
``resolve_dispatch_mode`` / ``persist_dispatch_mode`` (mode selection),
``_team_member_names`` (first-touch detection), and ``_parse_reply_envelope``
(the kept fence-unescape helper used by ``status.py`` and the host poll).

Matrix covered here:

* sub-agent: ``dispatch_task("subagent", ...)`` records exactly ONE
  ``spawn_subagent`` and NOTHING team-related.
* agent-team FIRST-TOUCH (#59 gap): config.json WITHOUT the role's name =>
  ``spawn_teammate`` (an Agent spawn), NOT ``send_message``.
* agent-team SUBSEQUENT: config.json WITH the role's name => ``send_message``,
  NOT ``spawn_teammate``.
* missing config.json => first-touch (``spawn_teammate``).
* unknown mode => raises ``UnknownDispatchModeError`` (a ``DispatchError``).
* ``resolve_dispatch_mode``: env set vs. marker vs. default vs. bad value.
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
    dispatch_task,
    persist_dispatch_mode,
    resolve_dispatch_mode,
)

# ── Fake injected tool boundary (records every call; no real harness) ───────


class _FakeTools:
    """Records every DispatchTools call in order. ``create_team`` returns a
    fixed team_id so a once-per-cycle capture can be asserted."""

    def __init__(self, team_id: str = "team-xyz"):
        self._team_id = team_id
        self.calls: list[tuple] = []

    def create_team(self, name: str, members: list[str]) -> str:
        self.calls.append(("create_team", name, tuple(members)))
        return self._team_id

    def spawn_teammate(self, team_id: str, name: str, prompt: str, model=None) -> None:
        # model is APPENDED only when set, so a model-less spawn records a tuple
        # byte-identical to the pre-policy shape (back-compat for existing
        # assertions); model-aware tests assert the trailing element.
        call = ("spawn_teammate", team_id, name, prompt)
        self.calls.append((*call, model) if model is not None else call)

    def send_message(self, team_id: str, to: str, message: str) -> None:
        self.calls.append(("send_message", team_id, to, message))

    def spawn_subagent(self, task_id, attempt: int, prompt: str, model=None) -> None:
        call = ("spawn_subagent", task_id, attempt, prompt)
        self.calls.append((*call, model) if model is not None else call)

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


# ── sub-agent path: dispatch_task spawn_subagent fire-and-forget only ────────


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
    # NOTHING team-related.
    assert "create_team" not in tools.names()
    assert "spawn_teammate" not in tools.names()
    assert "send_message" not in tools.names()


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


# ── model-tier threading through dispatch_task (atelier) ─────────────────────


def test_dispatch_task_threads_model_into_subagent_spawn(tmp_path):
    """A non-None model is threaded into the spawn_subagent call (model-tier
    selection); a model-less dispatch is byte-identical to the pre-policy shape."""
    tools = _FakeTools()
    dispatch_task(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        task={"id": "t1"},
        attempt=1,
        briefing="B",
        teams_root=tmp_path,
        model="opus",
    )
    assert tools.calls == [("spawn_subagent", "t1", 1, "B", "opus")]


def test_dispatch_task_no_model_is_byte_identical(tmp_path):
    """No model => no trailing model element (back-compat)."""
    tools = _FakeTools()
    dispatch_task(
        DISPATCH_MODE_SUBAGENT,
        tools=tools,
        task={"id": "t1"},
        attempt=1,
        briefing="B",
        teams_root=tmp_path,
    )
    assert tools.calls == [("spawn_subagent", "t1", 1, "B")]


def test_dispatch_task_threads_model_into_first_touch(tmp_path):
    """In agent-team first-touch the model is threaded into spawn_teammate."""
    tools = _FakeTools(team_id="T")
    dispatch_task(
        DISPATCH_MODE_AGENT_TEAM,
        tools=tools,
        task={"id": "t1"},
        attempt=1,
        briefing="B",
        team_id="T",
        teammate_name="sdet-1",
        teams_root=tmp_path,
        model="sonnet",
    )
    assert tools.calls == [("spawn_teammate", "T", "sdet-1", "B", "sonnet")]


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


# ── fence round-trip: _parse_reply_envelope reverses bridge_read._fence ──────
#
# _parse_reply_envelope is KEPT (the message-WIRE fence helper; used by
# status.py and the host poll). It is unrelated to the removed dispatch queue.


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
