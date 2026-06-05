"""Tests for scripts/loom_comms.py — the gated Loom team-chat wrapper.

These drive the REAL production callers — :func:`compose_briefing` (the
template render), :func:`detect` (the availability gate), and the PM-side
:func:`kickoff` / :func:`invite` / :func:`deregister` helpers — not isolated
units. Loom itself is NEVER contacted: every subprocess call is intercepted by
an injected fake runner that returns ``CompletedProcess``-likes mirroring the
REAL ``loom_chat.py`` CLI contract:

* ``detect`` prints ``{"available": ...}``,
* ``register`` prints ``{"assigned_name": ...}``,
* ``send`` / ``create-channel`` / ``join`` / ``deregister`` print server JSON.

The architecture invariant under test: Loom carries PEER chat + the kickoff
meeting + goals; the BRIDGE always carries the terminal ``task_result`` reply
envelope (TM-006). The fallback path (Loom unavailable) is byte-identical to
bridge-only and NEVER raises.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.dispatch import REQUIRED_VARS, compose_briefing, validate_render_context
from scripts.loom_comms import (
    DEFAULT_MAX_BODY,
    LoomStatus,
    build_team_chat_context,
    deregister,
    detect,
    invite,
    kickoff,
    loom_cmds,
    resolve_loom_client,
)

# ---------------------------------------------------------------------------
# Fake subprocess runner — mirrors the real loom_chat.py CLI contract
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records every argv and returns scripted ``CompletedProcess``-likes.

    Replays the REAL ``loom_chat.py`` CLI contract by subcommand so the wrapper
    is exercised against the contract the live client actually honors, not an
    invented one. ``calls`` accumulates every argv for exact-count assertions.
    """

    def __init__(self, *, available: bool = True, fail_cmds: set[str] | None = None) -> None:
        self.available = available
        self.fail_cmds = fail_cmds or set()
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=True, text=True, check=False, **kwargs):
        self.calls.append(list(argv))
        # argv == [python, <client>, <subcommand>, ...]; the subcommand is [2].
        cmd = argv[2] if len(argv) > 2 else ""
        if cmd in self.fail_cmds:
            return subprocess.CompletedProcess(argv, 4, stdout='{"error": "boom"}', stderr="")
        if cmd == "detect":
            if self.available:
                payload = {
                    "available": True,
                    "url": "http://127.0.0.1:7077/mcp",
                    "port": 7077,
                    "source": "endpoint-file",
                }
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
            return subprocess.CompletedProcess(argv, 3, stdout='{"available": false}', stderr="")
        if cmd == "register":
            name = argv[3] if len(argv) > 3 else "agent"
            out = {
                "assigned_name": name,
                "session_id": "sid-123",
                "url": "http://127.0.0.1:7077/mcp",
            }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")
        if cmd in ("create-channel", "join", "send", "deregister", "inbox"):
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"ok": True, "cmd": cmd}), stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


def _send_calls(runner: FakeRunner) -> list[list[str]]:
    return [c for c in runner.calls if len(c) > 2 and c[2] == "send"]


# ---------------------------------------------------------------------------
# resolve_loom_client
# ---------------------------------------------------------------------------


def test_resolve_loom_client_env_override(tmp_path: Path) -> None:
    """LOOM_CLIENT override is honored when the path exists."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake client\n", encoding="utf-8")
    resolved = resolve_loom_client(env={"LOOM_CLIENT": str(client)})
    assert resolved == client


def test_resolve_loom_client_env_override_dangling_falls_through(tmp_path, monkeypatch) -> None:
    """A set-but-nonexistent LOOM_CLIENT does not wedge discovery — it falls
    through to cache discovery (None here, with an empty cache root) rather than
    returning the dangling path."""
    import scripts.loom_comms as lc

    monkeypatch.setattr(lc, "_AGORA_CACHE_ROOT", tmp_path / "empty-cache")
    missing = tmp_path / "nope.py"
    resolved = resolve_loom_client(env={"LOOM_CLIENT": str(missing)})
    assert resolved is None


def test_resolve_loom_client_agora_cache_highest_version(tmp_path, monkeypatch) -> None:
    """Agora-cache discovery picks the highest version dir whose client exists.

    Seeds 0.1.0, 0.2.0, and 0.10.0 — the numeric sort MUST pick 0.10.0, not a
    lexical 0.2.0. Validates by patching the module's cache root to tmp_path.
    """
    import scripts.loom_comms as lc

    root = tmp_path / "loom-agent-chat"
    for ver in ("0.1.0", "0.2.0", "0.10.0"):
        d = root / ver / "skills" / "loom-chat"
        d.mkdir(parents=True)
        (d / "loom_chat.py").write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(lc, "_AGORA_CACHE_ROOT", tmp_path)
    resolved = resolve_loom_client(env={})
    assert resolved == root / "0.10.0" / "skills" / "loom-chat" / "loom_chat.py"


def test_resolve_loom_client_none_when_absent(tmp_path, monkeypatch) -> None:
    """No override + empty cache => None (loom-agent-chat not installed)."""
    import scripts.loom_comms as lc

    monkeypatch.setattr(lc, "_AGORA_CACHE_ROOT", tmp_path / "empty")
    assert resolve_loom_client(env={}) is None


def test_resolve_loom_client_skips_version_dir_without_client(tmp_path, monkeypatch) -> None:
    """A higher version dir WITHOUT the client file is skipped for a lower one
    that has it — we validate the file, not just the dir name."""
    import scripts.loom_comms as lc

    root = tmp_path / "loom-agent-chat"
    # 0.9.0 has no client file; 0.1.0 does.
    (root / "0.9.0" / "skills" / "loom-chat").mkdir(parents=True)
    low = root / "0.1.0" / "skills" / "loom-chat"
    low.mkdir(parents=True)
    (low / "loom_chat.py").write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(lc, "_AGORA_CACHE_ROOT", tmp_path)
    assert resolve_loom_client(env={}) == low / "loom_chat.py"


# ---------------------------------------------------------------------------
# detect — the availability GATE (req 1), fail-soft
# ---------------------------------------------------------------------------


def test_detect_available_exit0(tmp_path: Path) -> None:
    """exit 0 + {"available": true} => LoomStatus(available=True) with URL/port."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")
    runner = FakeRunner(available=True)
    status = detect(client=client, runner=runner)
    assert status.available is True
    assert status.url == "http://127.0.0.1:7077/mcp"
    assert status.port == 7077
    assert status.source == "endpoint-file"
    assert status.client == client


def test_detect_unavailable_exit3(tmp_path: Path) -> None:
    """exit 3 ({"available": false}) => available=False (fail-soft fallback)."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")
    status = detect(client=client, runner=FakeRunner(available=False))
    assert status.available is False


def test_detect_client_missing_unavailable(tmp_path, monkeypatch) -> None:
    """No client resolvable (loom-agent-chat absent) => available=False; the
    runner is never even invoked."""
    import scripts.loom_comms as lc

    monkeypatch.setattr(lc, "_AGORA_CACHE_ROOT", tmp_path / "empty")
    runner = FakeRunner(available=True)
    status = detect(runner=runner, env={})
    assert status.available is False
    assert runner.calls == []  # short-circuited before any subprocess


def test_detect_malformed_stdout_unavailable(tmp_path: Path) -> None:
    """exit 0 but unparseable stdout => available=False (fail-soft)."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")

    def bad_runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="not json at all", stderr="")

    assert detect(client=client, runner=bad_runner).available is False


def test_detect_empty_stdout_unavailable(tmp_path: Path) -> None:
    """exit 0 with empty stdout => available=False."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")

    def empty_runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert detect(client=client, runner=empty_runner).available is False


def test_detect_available_false_key_unavailable(tmp_path: Path) -> None:
    """exit 0 but JSON {"available": false} => available=False (gate on BOTH
    exit code AND the json flag)."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout='{"available": false}', stderr="")

    assert detect(client=client, runner=runner).available is False


def test_detect_runner_raises_unavailable(tmp_path: Path) -> None:
    """A runner that RAISES (e.g. the binary vanished) => available=False,
    NEVER propagates (the never-crash gate)."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")

    def raising_runner(argv, **kw):
        raise OSError("boom — exec failed")

    assert detect(client=client, runner=raising_runner).available is False


def test_detect_nonzero_exit_with_available_true_payload(tmp_path: Path) -> None:
    """The EXIT-CODE guard in isolation: a non-zero exit (returncode=1) with an
    {"available": true} stdout STILL yields available=False — the gate requires
    BOTH exit 0 AND the json flag, so a crashing client that happens to print a
    truthy payload does not slip through."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout='{"available": true}', stderr="")

    assert detect(client=client, runner=runner).available is False


@pytest.mark.parametrize("available_value", [1, "true"])
def test_detect_truthy_but_not_true_available_is_false(tmp_path, available_value) -> None:
    """The strict ``is True`` check: a truthy-but-not-True ``available`` (the int
    1 or the string "true") is REJECTED — only the JSON boolean ``true`` passes.
    Pins the gate against a sloppy ``if payload.get("available")`` truthiness
    test that would accept these."""
    client = tmp_path / "loom_chat.py"
    client.write_text("# fake\n", encoding="utf-8")

    def runner(argv, **kw):
        payload = json.dumps({"available": available_value})
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    assert detect(client=client, runner=runner).available is False


# ---------------------------------------------------------------------------
# build_team_chat_context — ALWAYS a dict, never None
# ---------------------------------------------------------------------------


def test_build_team_chat_context_unavailable_is_bridge() -> None:
    """An unavailable status => {"transport": "bridge"} (the fallback ctx)."""
    ctx = build_team_chat_context(
        LoomStatus(available=False), role_id="be-1", channel="cycle-1", team_lead_name="team-lead"
    )
    assert ctx == {"transport": "bridge"}


def test_build_team_chat_context_available_is_loom() -> None:
    """An available status => a loom-transport ctx carrying the cmds + cap."""
    ctx = build_team_chat_context(
        LoomStatus(available=True, client=Path("/x/loom_chat.py")),
        role_id="be-1",
        channel="cycle-1",
        team_lead_name="team-lead",
    )
    assert ctx["transport"] == "loom"
    assert ctx["channel"] == "cycle-1"
    assert ctx["max_body"] == DEFAULT_MAX_BODY
    assert "register" in ctx["cmds"]
    assert "be-1" in ctx["cmds"]["register"]


def test_build_team_chat_context_never_none() -> None:
    """Both paths return a dict — NEVER None — so validate_render_context (which
    treats None as missing) passes on the fallback."""
    for status in (LoomStatus(available=True), LoomStatus(available=False)):
        ctx = build_team_chat_context(status, role_id="r", channel="c", team_lead_name="tl")
        assert isinstance(ctx, dict)


# ---------------------------------------------------------------------------
# loom_cmds — the rendered command templates
# ---------------------------------------------------------------------------


def test_loom_cmds_keys_and_doc_spill() -> None:
    """loom_cmds carries every key the template renders, plus the doc-spill
    rule that names the cap."""
    cmds = loom_cmds(
        role_id="be-1",
        channel="cycle-1",
        client="/abs/loom_chat.py",
        team_lead_name="team-lead",
        max_body=500,
    )
    for key in (
        "register",
        "send_to_peer",
        "send_to_lead",
        "read_inbox",
        "mark_read",
        "deregister",
        "channel",
        "max_body",
        "doc_spill",
    ):
        assert key in cmds, f"loom_cmds missing key {key!r}"
    assert cmds["channel"] == "cycle-1"
    assert cmds["max_body"] == "500"
    assert "500" in cmds["doc_spill"]
    assert ".loom/temp/" in cmds["doc_spill"]
    assert "register be-1" in cmds["register"]


def test_loom_cmds_are_runnable_no_placeholders() -> None:
    """The rendered commands are RUNNABLE: the resolved client path + the
    concrete lead handle are substituted, and NO <loom_client> /
    <team_lead_role_id> placeholder survives. Only the legitimately
    caller-filled <peer_role_id> / <body> / <id> slots remain."""
    cmds = loom_cmds(
        role_id="be-1",
        channel="cycle-1",
        client="/abs/loom_chat.py",
        team_lead_name="team-lead",
    )
    # Concrete client path baked into every command.
    assert cmds["register"] == "python3 /abs/loom_chat.py register be-1"
    # Concrete lead handle in send_to_lead — NOT a <team_lead_role_id> placeholder.
    assert "team-lead" in cmds["send_to_lead"]
    assert "<team_lead_role_id>" not in cmds["send_to_lead"]
    # The peer send KEEPS the caller-chosen <peer_role_id> placeholder.
    assert "<peer_role_id>" in cmds["send_to_peer"]
    # NO <loom_client> placeholder anywhere.
    for value in cmds.values():
        assert "<loom_client>" not in value
        assert "<team_lead_role_id>" not in value


# ---------------------------------------------------------------------------
# FALLBACK path — compose_briefing WITHOUT team_chat (bridge only)
# ---------------------------------------------------------------------------


def _compose_kwargs(**overrides):
    base = {
        "role_id": "backend-engineer-1",
        "task_id": 7,
        "persona_profile_text": "You are a backend engineer.",
        "phase_procedure_text": "Implement the module.",
        "task_brief": "Build scripts/loom_comms.py.",
        "team_id": "cycle-1-team",
        "team_lead_name": "team-lead",
        "wave_id": "wave-1",
        "wave_phase": "implement",
        "deadline_iso": "2099-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_required_vars_contains_team_chat() -> None:
    """team_chat is a declared REQUIRED_VAR (so the AST-union test + the
    pre-render validator stay consistent)."""
    assert "team_chat" in REQUIRED_VARS


def test_compose_without_team_chat_renders_bridge_no_loom() -> None:
    """compose_briefing WITHOUT team_chat coerces to the bridge fallback: the
    CHANNELS block renders, but NO Loom SUBSECTION — assert the rendered loom
    register command string is ABSENT and the bridge wiring present.

    NOTE: the verbatim team-mode-rules block (prepended to every briefing) now
    *mentions* Loom in prose, so we anchor on a marker the TEMPLATE'S Loom
    subsection emits and the rules-block prose never does: the rendered
    `register <role-id>` command (from loom_cmds) and the subsection's
    distinctive table heading."""
    out = compose_briefing(**_compose_kwargs())
    assert "# CHANNELS" in out
    # The TEMPLATE Loom subsection (rendered only on the loom path) is ABSENT.
    assert "register backend-engineer-1" not in out  # no loom_cmds register cmd
    assert "Loom team-chat (PEER chat" not in out  # the subsection heading
    assert "deregister --as backend-engineer-1" not in out  # no loom deregister cmd
    # The bridge control-plane wiring IS present.
    assert "bridge_send.py" in out
    assert "Send to team-lead" in out


def test_validate_render_context_bridge_fallback_does_not_raise() -> None:
    """The bridge-fallback team_chat dict satisfies validate_render_context —
    team_chat is a non-None dict, so None==missing never trips."""
    out = compose_briefing(**_compose_kwargs(team_chat={"transport": "bridge"}))
    assert "# CHANNELS" in out
    # And the validator itself does not raise on a representative full context.
    ctx = {
        name: ("x" if name != "team_chat" else {"transport": "bridge"}) for name in REQUIRED_VARS
    }
    validate_render_context(ctx)  # must not raise


# ---------------------------------------------------------------------------
# LOOM path — compose_briefing WITH a loom team_chat (both transports present)
# ---------------------------------------------------------------------------


def test_compose_with_loom_renders_loom_block_and_keeps_bridge() -> None:
    """compose_briefing WITH a loom team_chat renders the Loom block (register
    <role-id>, <=500 rule, deregister) AND STILL renders the bridge reply-
    envelope wiring — Loom does NOT replace the control-plane."""
    team_chat = build_team_chat_context(
        LoomStatus(available=True, client=Path("/x/loom_chat.py")),
        role_id="backend-engineer-1",
        channel="cycle-1",
        team_lead_name="team-lead",
    )
    out = compose_briefing(**_compose_kwargs(team_chat=team_chat))
    # Loom block present.
    assert "Loom team-chat" in out
    assert "register backend-engineer-1" in out
    assert "500 chars" in out
    assert "deregister --as backend-engineer-1" in out
    assert "cycle-1" in out
    # Bridge control-plane STILL present — the reply envelope rides the bridge.
    assert "bridge_send.py" in out
    assert "task_result" in out  # TM-006 reply envelope still in REPLY CONTRACT
    # The explicit invariant note.
    assert "Loom never carries" in out or "Loom NEVER carries" in out


def test_compose_with_loom_renders_runnable_commands_no_placeholders() -> None:
    """Regression guard for the placeholder-resolution fix: a worker briefing
    rendered with an AVAILABLE LoomStatus whose client is a concrete path
    contains RUNNABLE commands — the concrete `register <role-id>` and a
    `send <channel> team-lead` lead-chat command — and does NOT leak the literal
    `<loom_client>` / `<team_lead_role_id>` placeholders into the prompt."""
    team_chat = build_team_chat_context(
        LoomStatus(available=True, client=Path("/abs/path/loom_chat.py")),
        role_id="backend-engineer-1",
        channel="cycle-1",
        team_lead_name="team-lead",
    )
    out = compose_briefing(**_compose_kwargs(team_chat=team_chat))
    # Concrete, runnable register command (resolved client path baked in).
    assert "python3 /abs/path/loom_chat.py register backend-engineer-1" in out
    # The lead-chat command addresses the concrete `team-lead` handle.
    assert "send cycle-1 team-lead" in out
    # NO <loom_client> placeholder survives anywhere in the prompt (it is a
    # Loom-only token — the bridge block never emits it).
    assert "<loom_client>" not in out
    # The <team_lead_role_id> placeholder MUST NOT leak into the Loom subsection
    # (fix #1). It still legitimately appears in the BRIDGE block's send_to_lead
    # (`_default_bridge_cmds` resolves the lead role-id on the bridge side), so
    # we scope the assertion to the Loom subsection only.
    loom_section = out.split("## Loom team-chat", 1)[1].split("# REPLY CONTRACT", 1)[0]
    assert "<team_lead_role_id>" not in loom_section
    assert "<loom_client>" not in loom_section


# ---------------------------------------------------------------------------
# kickoff WIRING — exact-count team + per-member directed posts
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> Path:
    c = tmp_path / "loom_chat.py"
    c.write_text("# fake\n", encoding="utf-8")
    return c


def test_kickoff_posts_team_goal_atHere_and_one_per_member(tmp_path: Path) -> None:
    """kickoff posts the TEAM goal to @here AND exactly one DIRECTED individual
    goal per member. Exact-count assertions so a silent drop of the team goal OR
    any individual goal FAILS the test."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    members = ["be-1", "sdet-1", "sec-1"]
    individual = {
        "be-1": "Implement loom_comms.",
        "sdet-1": "Write the loom_comms tests.",
        "sec-1": "Security-review the subprocess seam.",
    }
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal="Ship loom team comms.",
        individual_goals=individual,
        members=members,
        runner=runner,
    )
    assert result["transport"] == "loom"
    assert result["posted"] is True
    # 1 team @here + 3 directed = 4 sends.
    assert result["messages"] == 4

    sends = _send_calls(runner)
    # Exactly one @here team-goal send.
    here_sends = [c for c in sends if c[4] == "@here"]
    assert len(here_sends) == 1, f"expected exactly one @here send, got {here_sends}"
    assert "Ship loom team comms." in here_sends[0][5]

    # Exactly one directed send per member, addressed to that member by name.
    for member in members:
        directed = [c for c in sends if c[4] == member]
        assert len(directed) == 1, f"expected exactly one directed send to {member}, got {directed}"
        assert individual[member] in directed[0][5]

    # Total sends == 4 (no extras, no drops).
    assert len(sends) == 4


def test_kickoff_registers_pm_and_creates_channel(tmp_path: Path) -> None:
    """kickoff registers the PM and creates the channel before posting."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    kickoff(
        status=status,
        channel="cycle-1",
        team_goal="goal",
        individual_goals={"be-1": "do it"},
        members=["be-1"],
        pm_name="team-lead",
        runner=runner,
    )
    cmds = [c[2] for c in runner.calls]
    assert "register" in cmds
    assert "create-channel" in cmds
    # register fires before the first send.
    assert cmds.index("register") < cmds.index("send")
    # create-channel fires before the first send too — moving create-channel
    # AFTER the goal sends (posting into a channel that does not exist yet) goes
    # RED here.
    assert cmds.index("create-channel") < cmds.index("send")
    # Every send is posted under the PM's --as name (the refactor lock).
    for c in _send_calls(runner):
        assert c[-2:] == ["--as", "team-lead"]


def test_kickoff_posts_under_custom_pm_name(tmp_path: Path) -> None:
    """A custom pm_name threads through to the `--as` of every register/send —
    no module-global leakage across calls."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    kickoff(
        status=status,
        channel="cycle-1",
        team_goal="goal",
        individual_goals={"be-1": "do it"},
        members=["be-1"],
        pm_name="planner",
        runner=runner,
    )
    assert runner.calls[0][2] == "register"
    assert runner.calls[0][3] == "planner"
    for c in _send_calls(runner):
        assert c[-2:] == ["--as", "planner"]


def test_kickoff_fallback_posts_nothing_when_unavailable() -> None:
    """kickoff with an unavailable status posts NOTHING and returns posted=False
    (the caller falls back to the bridge meeting)."""
    runner = FakeRunner(available=False)
    result = kickoff(
        status=LoomStatus(available=False),
        channel="cycle-1",
        team_goal="goal",
        individual_goals={"be-1": "x"},
        members=["be-1"],
        runner=runner,
    )
    assert result == {"transport": "bridge", "posted": False}
    assert runner.calls == []  # zero subprocess calls on the fallback path


def test_kickoff_skips_member_without_individual_goal(tmp_path: Path) -> None:
    """A member with no entry in individual_goals is skipped (no directed send),
    not posted with an empty body."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal="goal",
        individual_goals={"be-1": "do it"},  # sdet-1 omitted
        members=["be-1", "sdet-1"],
        runner=runner,
    )
    sends = _send_calls(runner)
    # 1 @here + 1 directed (be-1 only) = 2.
    assert result["messages"] == 2
    assert not any(c[4] == "sdet-1" for c in sends)


def test_kickoff_duplicate_members_raises(tmp_path: Path) -> None:
    """A duplicate role-id in the members roster raises ValueError (fix #3): the
    per-cycle collision-free assumption is load-bearing, so a duplicate that
    could trigger a Loom server collision-rename fails loud rather than silently
    mis-addressing a directed send."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    with pytest.raises(ValueError, match="duplicate-free"):
        kickoff(
            status=status,
            channel="cycle-1",
            team_goal="goal",
            individual_goals={"be-1": "do it"},
            members=["be-1", "be-1"],
            runner=runner,
        )


# ---------------------------------------------------------------------------
# doc-spill — a goal > max_body triggers a temp-file write + pointer post
# ---------------------------------------------------------------------------


def test_kickoff_doc_spills_long_individual_goal(tmp_path: Path) -> None:
    """An individual goal > max_body is written to temp_dir/<slug>.md and a SHORT
    pointer (NOT the full body) is sent. Assert the temp file exists with the
    full body, and the sent body is the pointer, not the body."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    temp_dir = tmp_path / "loomtemp"
    long_goal = "X" * 800  # > DEFAULT_MAX_BODY (500)
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal="short team goal",
        individual_goals={"be-1": long_goal},
        members=["be-1"],
        runner=runner,
        temp_dir=str(temp_dir),
    )
    # The temp file was written with the FULL body.
    spill_path = temp_dir / "be-1.md"
    assert spill_path.exists()
    assert spill_path.read_text(encoding="utf-8") == long_goal
    assert str(spill_path) in result["spilled"]

    # The directed send to be-1 carried a POINTER, not the 800-char body.
    sends = _send_calls(runner)
    directed = next(c for c in sends if c[4] == "be-1")
    sent_body = directed[5]
    assert long_goal not in sent_body  # full body NOT pasted into chat
    assert str(spill_path) in sent_body  # pointer references the file
    assert len(sent_body) <= DEFAULT_MAX_BODY


def test_kickoff_doc_spills_long_team_goal(tmp_path: Path) -> None:
    """The TEAM goal is doc-spilled too when it exceeds the cap."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    temp_dir = tmp_path / "loomtemp"
    long_team = "T" * 900
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal=long_team,
        individual_goals={},
        members=[],
        runner=runner,
        temp_dir=str(temp_dir),
    )
    spill_path = temp_dir / "team-goal.md"
    assert spill_path.exists()
    assert str(spill_path) in result["spilled"]
    here = next(c for c in _send_calls(runner) if c[4] == "@here")
    assert long_team not in here[5]
    assert str(spill_path) in here[5]
    # Symmetry with the individual-goal spill test: the pointer body fits the cap.
    assert len(here[5]) <= DEFAULT_MAX_BODY


# ---------------------------------------------------------------------------
# invite (req 8) + deregister (req 7) — fail-soft
# ---------------------------------------------------------------------------


def test_invite_registers_and_joins(tmp_path: Path) -> None:
    """invite registers the new role-id then joins it to the channel (req 8)."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = invite(status=status, channel="cycle-1", role_id="qa-1", runner=runner)
    assert result == {"transport": "loom", "invited": "qa-1", "joined": True}
    cmds = [c[2] for c in runner.calls]
    assert "register" in cmds
    assert "join" in cmds


def test_invite_fallback_when_unavailable() -> None:
    """invite on an unavailable Loom is fail-soft: joined=False, no subprocess."""
    runner = FakeRunner(available=False)
    result = invite(status=LoomStatus(available=False), channel="c", role_id="qa-1", runner=runner)
    assert result == {"transport": "bridge", "invited": "qa-1", "joined": False}
    assert runner.calls == []


def test_deregister_marks_gone(tmp_path: Path) -> None:
    """deregister marks the agent gone (history retained) — req 7."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = deregister(status=status, name="be-1", runner=runner)
    assert result == {"transport": "loom", "deregistered": True, "name": "be-1"}
    assert runner.calls[-1][2] == "deregister"
    assert runner.calls[-1][3:] == ["--as", "be-1"]


def test_deregister_fallback_when_unavailable() -> None:
    """deregister is fail-soft on an unavailable Loom."""
    runner = FakeRunner(available=False)
    result = deregister(status=LoomStatus(available=False), name="be-1", runner=runner)
    assert result == {"transport": "bridge", "deregistered": False, "name": "be-1"}
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Fail-soft helpers never raise on a transport error
# ---------------------------------------------------------------------------


def test_kickoff_failed_send_is_soft_not_raised(tmp_path: Path) -> None:
    """A failing `send` (exit 4) does not raise — the goal is just 'not posted'
    and the messages count reflects only what landed."""
    runner = FakeRunner(available=True, fail_cmds={"send"})
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal="goal",
        individual_goals={"be-1": "x"},
        members=["be-1"],
        runner=runner,
    )
    # No send succeeded → messages == 0, but posted is True (we reached Loom).
    assert result["posted"] is True
    assert result["messages"] == 0


def test_helpers_never_raise_on_runner_exception(tmp_path: Path) -> None:
    """Every PM-side helper swallows a raising runner (never-crash gate)."""

    def raising(argv, **kw):
        raise OSError("transport down mid-call")

    status = LoomStatus(available=True, client=_client(tmp_path))
    # None of these should raise.
    kickoff(
        status=status,
        channel="c",
        team_goal="g",
        individual_goals={"be-1": "x"},
        members=["be-1"],
        runner=raising,
    )
    invite(status=status, channel="c", role_id="qa-1", runner=raising)
    deregister(status=status, name="be-1", runner=raising)


def test_detect_via_resolve_real_client_smoke(tmp_path, monkeypatch) -> None:
    """End-to-end gate smoke: detect() resolves the client through
    resolve_loom_client (agora cache) and runs it through the injected runner —
    exercising the real production resolution path, not a hand-fed client."""
    import scripts.loom_comms as lc

    root = tmp_path / "loom-agent-chat" / "0.1.0" / "skills" / "loom-chat"
    root.mkdir(parents=True)
    (root / "loom_chat.py").write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(lc, "_AGORA_CACHE_ROOT", tmp_path)
    runner = FakeRunner(available=True)
    status = detect(runner=runner, env={})
    assert status.available is True
    assert status.client == root / "loom_chat.py"
    # The runner was invoked with the resolved client path.
    assert runner.calls[0][1] == str(root / "loom_chat.py")
    assert runner.calls[0][2] == "detect"


def test_no_shell_true_anywhere(tmp_path: Path) -> None:
    """Defense-in-depth: assert the runner is NEVER called with shell=True — all
    invocations are list-form argv (the untrusted-input boundary)."""
    seen_kwargs: list[dict] = []

    def recording(argv, **kw):
        seen_kwargs.append(kw)
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"available": True}), stderr=""
        )

    client = _client(tmp_path)
    detect(client=client, runner=recording)
    status = LoomStatus(available=True, client=client)
    kickoff(
        status=status,
        channel="c",
        team_goal="g",
        individual_goals={"be-1": "x"},
        members=["be-1"],
        runner=recording,
    )
    invite(status=status, channel="c", role_id="qa-1", runner=recording)
    deregister(status=status, name="be-1", runner=recording)
    assert seen_kwargs, "expected at least one runner invocation"
    assert all("shell" not in kw for kw in seen_kwargs)


def test_slug_neutralizes_path_traversal(tmp_path: Path) -> None:
    """A malicious member name with path separators cannot escape temp_dir —
    the doc-spill slug strips separators so the write stays inside temp_dir."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    temp_dir = tmp_path / "loomtemp"
    evil = "../../etc/be"
    kickoff(
        status=status,
        channel="cycle-1",
        team_goal="short",
        individual_goals={evil: "Y" * 800},
        members=[evil],
        runner=runner,
        temp_dir=str(temp_dir),
    )
    # No file was written OUTSIDE temp_dir.
    written = list(temp_dir.rglob("*.md"))
    assert written, "expected a spill file inside temp_dir"
    for p in written:
        assert temp_dir in p.resolve().parents or p.parent == temp_dir


def test_kickoff_short_goals_no_spill(tmp_path: Path) -> None:
    """Goals under the cap are NOT spilled — spilled list is empty and the body
    is sent verbatim."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal="short team",
        individual_goals={"be-1": "short member"},
        members=["be-1"],
        runner=runner,
        temp_dir=str(tmp_path / "t"),
    )
    assert result["spilled"] == []
    directed = next(c for c in _send_calls(runner) if c[4] == "be-1")
    assert directed[5] == "short member"


def test_max_body_override_respected(tmp_path: Path) -> None:
    """A custom max_body changes the spill threshold."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    temp_dir = tmp_path / "t"
    body = "Z" * 50  # 50 chars
    result = kickoff(
        status=status,
        channel="cycle-1",
        team_goal="short",
        individual_goals={"be-1": body},
        members=["be-1"],
        runner=runner,
        temp_dir=str(temp_dir),
        max_body=40,  # 50 > 40 → spill
    )
    assert (temp_dir / "be-1.md").exists()
    assert str(temp_dir / "be-1.md") in result["spilled"]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("be-1", "be-1"),
        ("sdet_1", "sdet_1"),
        ("a/b/c", "a-b-c"),
        ("../../x", "x"),
        ("", "goal"),
    ],
)
def test_slug_table(name, expected) -> None:
    from scripts.loom_comms import _slug

    assert _slug(name) == expected
