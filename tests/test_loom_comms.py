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
    TEARDOWN_COLLISION_SWEEP_MAX,
    LoomStatus,
    _run_loom_raw,
    build_team_chat_context,
    deregister,
    detect,
    invite,
    kickoff,
    loom_cmds,
    rejoin,
    resolve_loom_client,
    teardown,
)

#: Number of collision variants swept per name (<name>-2 .. <name>-MAX).
_VARIANTS_PER_NAME = TEARDOWN_COLLISION_SWEEP_MAX - 1

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


def test_teardown_deregisters_all_participants(tmp_path: Path) -> None:
    """teardown sweeps pm + every member, marking each gone (history retained).
    The verbatim base sweep fires FIRST, in roster order, before any collision
    variant — the `attempted`/`deregistered` keys keep base-name-only semantics."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = teardown(status=status, members=["be-1", "fe-1", "qa-1"], runner=runner)
    assert result["transport"] == "loom"
    assert result["attempted"] == ["team-lead", "be-1", "fe-1", "qa-1"]
    assert result["deregistered"] == ["team-lead", "be-1", "fe-1", "qa-1"]
    dereg = [c[3:] for c in runner.calls if c[2] == "deregister"]
    # Base names sweep first, verbatim, in order; variants only after.
    assert dereg[:4] == [
        ["--as", "team-lead"],
        ["--as", "be-1"],
        ["--as", "fe-1"],
        ["--as", "qa-1"],
    ]
    # Every later deregister is a collision variant of a base name.
    assert all(
        args[1].rsplit("-", 1)[0] in ("team-lead", "be-1", "fe-1", "qa-1") for args in dereg[4:]
    )


def test_teardown_dedups_pm_in_members(tmp_path: Path) -> None:
    """A pm_name that also appears in members is swept exactly once — base AND
    variant sweeps both dedupe."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = teardown(status=status, members=["lead", "be-1"], pm_name="lead", runner=runner)
    assert result["attempted"] == ["lead", "be-1"]
    dereg = [c for c in runner.calls if c[2] == "deregister"]
    # 2 deduped base names + their collision variants — NOT 3 names' worth.
    assert len(dereg) == 2 * (1 + _VARIANTS_PER_NAME)
    # No name (base or variant) is deregistered twice.
    swept = [c[4] for c in dereg]
    assert len(swept) == len(set(swept))


def test_teardown_fallback_when_unavailable() -> None:
    """teardown on an unavailable Loom is fail-soft: no subprocess, nothing swept."""
    runner = FakeRunner(available=False)
    result = teardown(status=LoomStatus(available=False), members=["be-1"], runner=runner)
    assert result == {
        "transport": "bridge",
        "deregistered": [],
        "attempted": [],
        "variants_attempted": [],
        "variants_deregistered": [],
    }
    assert runner.calls == []


def test_teardown_failures_are_soft_and_never_abort(tmp_path: Path) -> None:
    """A failed deregister never aborts the sweep — every name is still attempted,
    and a name that did not confirm gone is simply absent from `deregistered`."""
    runner = FakeRunner(available=True, fail_cmds={"deregister"})
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = teardown(status=status, members=["be-1", "fe-1"], runner=runner)
    assert result["attempted"] == ["team-lead", "be-1", "fe-1"]
    assert result["deregistered"] == []
    assert result["variants_deregistered"] == []
    dereg = [c for c in runner.calls if c[2] == "deregister"]
    # All-failing deregisters still attempt every base name AND every variant.
    assert len(dereg) == 3 * (1 + _VARIANTS_PER_NAME)


def test_teardown_sweeps_collision_suffixed_variants(tmp_path: Path) -> None:
    """The prefix sweep deregisters the deterministic collision variants
    <name>-2..<name>-MAX of every swept name (pm included), in name order —
    so a re-registered worker the server renamed (be-1 → be-1-2) does not
    linger after teardown."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = teardown(status=status, members=["be-1"], runner=runner)
    expected_variants = [
        f"{name}-{n}"
        for name in ("team-lead", "be-1")
        for n in range(2, TEARDOWN_COLLISION_SWEEP_MAX + 1)
    ]
    assert result["variants_attempted"] == expected_variants
    # FakeRunner confirms every deregister → every variant confirms gone.
    assert result["variants_deregistered"] == expected_variants
    # Each variant got a REAL `deregister --as <variant>` subprocess call.
    dereg_names = [c[4] for c in runner.calls if c[2] == "deregister"]
    assert dereg_names == ["team-lead", "be-1", *expected_variants]


def test_teardown_variant_failure_does_not_abort_rest(tmp_path: Path) -> None:
    """One variant's failed deregister (the realistic never-registered exit 4)
    never aborts the sweep — later variants are still attempted and the failed
    one is simply absent from `variants_deregistered`."""

    base = FakeRunner(available=True)

    def runner(argv, **kw):
        # `deregister --as be-1-3` fails (never minted → loom_chat exit 4);
        # everything else succeeds.
        if argv[2] == "deregister" and argv[4] == "be-1-3":
            return subprocess.CompletedProcess(
                argv,
                4,
                stdout='{"error": "not registered as be-1-3 — run register first"}',
                stderr="",
            )
        return base(argv, **kw)

    status = LoomStatus(available=True, client=_client(tmp_path))
    result = teardown(status=status, members=["be-1"], runner=runner)
    assert "be-1-3" in result["variants_attempted"]
    assert "be-1-3" not in result["variants_deregistered"]
    # The variants AFTER the failure were still swept and confirmed.
    assert "be-1-4" in result["variants_deregistered"]
    assert result["deregistered"] == ["team-lead", "be-1"]


def test_teardown_collision_sweep_max_is_four() -> None:
    """Pin the literal sweep bound: every other teardown test derives its
    expectations FROM the constant, so mutating the value would otherwise pass
    the suite unnoticed. Changing this bound is a deliberate decision that
    requires updating this test (and the constant's rationale doc)."""
    assert TEARDOWN_COLLISION_SWEEP_MAX == 4


def test_teardown_member_that_is_already_a_variant_not_swept_twice(tmp_path: Path) -> None:
    """A roster name that IS a collision variant of another roster name (be-1 +
    be-1-2 both in members) is swept once as a base name and skipped by the
    variant sweep — no double deregister."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = teardown(status=status, members=["be-1", "be-1-2"], runner=runner)
    assert result["attempted"] == ["team-lead", "be-1", "be-1-2"]
    assert "be-1-2" not in result["variants_attempted"]
    dereg_names = [c[4] for c in runner.calls if c[2] == "deregister"]
    assert dereg_names.count("be-1-2") == 1


# ---------------------------------------------------------------------------
# rejoin — returning-agent path: join-first, stale-session re-register recovery
# ---------------------------------------------------------------------------

#: The EXACT stale-session failure shape the live loom client emits: the server
#: rejects the dead transport session with HTTP 400, loom_chat.py's error
#: boundary maps it to exit 4 with this stdout. rejoin's recovery trigger is the
#: NON-ZERO exit, not a "400" string match.
_STALE_EXIT = 4
_STALE_STDOUT = '{"error": "HTTPError: HTTP Error 400: Bad Request"}'


class StaleFirstJoinRunner(FakeRunner):
    """First `join` fails with the realistic stale-session shape (exit 4 +
    HTTPError-400 stdout); every later call follows the normal contract."""

    def __init__(self) -> None:
        super().__init__(available=True)
        self._join_seen = False

    def __call__(self, argv, **kw):
        if len(argv) > 2 and argv[2] == "join" and not self._join_seen:
            self._join_seen = True
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, _STALE_EXIT, stdout=_STALE_STDOUT, stderr="")
        return super().__call__(argv, **kw)


def test_rejoin_happy_path_no_redundant_register(tmp_path: Path) -> None:
    """A still-valid session re-joins directly: rejoined=True, reregistered=False,
    assigned_name == requested name, and NO register call is ever issued —
    exactly one subprocess (the join)."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="cycle-1", name="be-1", runner=runner)
    assert result == {
        "transport": "loom",
        "name": "be-1",
        "rejoined": True,
        "reregistered": False,
        "assigned_name": "be-1",
    }
    assert len(runner.calls) == 1
    assert runner.calls[0][2:] == ["join", "cycle-1", "--as", "be-1"]


def test_rejoin_stale_session_reregisters_then_rejoins(tmp_path: Path) -> None:
    """The stale-session recovery: first join fails (exit 4, the HTTPError-400
    stdout the live client emits) → re-register → second join succeeds. The
    contract is join-BEFORE-register: the first call MUST be the join attempt,
    register fires only after it failed."""
    runner = StaleFirstJoinRunner()
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="cycle-1", name="be-1", runner=runner)
    assert result == {
        "transport": "loom",
        "name": "be-1",
        "rejoined": True,
        "reregistered": True,
        # FakeRunner's register echoes the requested name (no collision) →
        # assigned_name degrades-to / equals the requested name.
        "assigned_name": "be-1",
    }
    # Exact call sequence: join (stale) → register → join. No extra calls.
    assert [c[2:] for c in runner.calls] == [
        ["join", "cycle-1", "--as", "be-1"],
        ["register", "be-1"],
        ["join", "cycle-1", "--as", "be-1"],
    ]


def test_rejoin_both_joins_fail_is_soft(tmp_path: Path) -> None:
    """Both joins failing never raises: rejoined=False, reregistered=True (the
    recovery path fired), and the full join → register → join sequence ran."""
    runner = FakeRunner(available=True, fail_cmds={"join"})
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="cycle-1", name="be-1", runner=runner)
    assert result == {
        "transport": "loom",
        "name": "be-1",
        "rejoined": False,
        "reregistered": True,
        "assigned_name": "be-1",
    }
    assert [c[2] for c in runner.calls] == ["join", "register", "join"]


def test_rejoin_fallback_when_unavailable() -> None:
    """rejoin on an unavailable Loom is fail-soft: bridge transport, no
    subprocess at all."""
    runner = FakeRunner(available=False)
    result = rejoin(status=LoomStatus(available=False), channel="c", name="be-1", runner=runner)
    assert result == {
        "transport": "bridge",
        "name": "be-1",
        "rejoined": False,
        "reregistered": False,
        "assigned_name": "be-1",
    }
    assert runner.calls == []


def test_rejoin_runner_raises_never_propagates(tmp_path: Path) -> None:
    """A runner that RAISES mid-rejoin (transport died) never propagates — the
    never-crash gate holds on the returning-agent path too."""

    def raising(argv, **kw):
        raise OSError("transport down mid-call")

    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="c", name="be-1", runner=raising)
    assert result["rejoined"] is False
    assert result["transport"] == "loom"


def test_deregister_rejoin_deregister_sequence_is_noop_safe(tmp_path: Path) -> None:
    """The full lifecycle round-trip — deregister → rejoin → deregister — is
    fail-soft no-op-safe in BOTH worlds: every transport call succeeding, and
    every join/deregister failing (the already-gone / never-registered case).
    Neither sequence raises."""
    status = LoomStatus(available=True, client=_client(tmp_path))

    # Happy world: all calls succeed.
    ok = FakeRunner(available=True)
    assert deregister(status=status, name="be-1", runner=ok)["deregistered"] is True
    assert rejoin(status=status, channel="c", name="be-1", runner=ok)["rejoined"] is True
    assert deregister(status=status, name="be-1", runner=ok)["deregistered"] is True

    # Degraded world: joins + deregisters all fail (exit 4) — pure no-ops.
    bad = FakeRunner(available=True, fail_cmds={"join", "deregister"})
    assert deregister(status=status, name="be-1", runner=bad)["deregistered"] is False
    result = rejoin(status=status, channel="c", name="be-1", runner=bad)
    assert result["rejoined"] is False
    assert result["reregistered"] is True
    assert deregister(status=status, name="be-1", runner=bad)["deregistered"] is False


class CollisionRenameRunner(FakeRunner):
    """Stale first join (exit 4, HTTPError-400 stdout) AND a server that
    collision-renames the recovery registration: ``register be-1`` mints
    ``be-1-2``. The recovery ``join`` succeeds ONLY ``--as be-1-2`` — joining
    as the stale ghost ``be-1`` keeps failing, exactly like the live server."""

    def __init__(self, *, requested: str, minted: str) -> None:
        super().__init__(available=True)
        self._requested = requested
        self._minted = minted

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        cmd = argv[2] if len(argv) > 2 else ""
        if cmd == "join":
            as_name = argv[argv.index("--as") + 1] if "--as" in argv else ""
            if as_name == self._minted:
                return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")
            return subprocess.CompletedProcess(argv, _STALE_EXIT, stdout=_STALE_STDOUT, stderr="")
        if cmd == "register":
            out = {
                "assigned_name": self._minted,
                "session_id": "sid-456",
                "url": "http://127.0.0.1:7077/mcp",
            }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


def test_rejoin_collision_rename_surfaces_assigned_name_and_rejoins_as_it(
    tmp_path: Path,
) -> None:
    """When the recovery register is collision-renamed (be-1 → be-1-2), rejoin
    MUST (a) issue the recovery join AS the minted name — joining as the old
    name addresses the stale ghost and fails — and (b) surface the minted name
    via ``assigned_name`` so peers direct-send to the LIVE identity."""
    runner = CollisionRenameRunner(requested="be-1", minted="be-1-2")
    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="cycle-1", name="be-1", runner=runner)
    assert result == {
        "transport": "loom",
        "name": "be-1",
        "rejoined": True,
        "reregistered": True,
        "assigned_name": "be-1-2",
    }
    # Exact sequence: join as requested (stale) → register → join AS MINTED name.
    assert [c[2:] for c in runner.calls] == [
        ["join", "cycle-1", "--as", "be-1"],
        ["register", "be-1"],
        ["join", "cycle-1", "--as", "be-1-2"],
    ]


@pytest.mark.parametrize(
    "register_stdout",
    [
        "not json at all",  # unparseable
        "",  # empty
        '{"session_id": "sid-789"}',  # parseable but no assigned_name key
        '{"assigned_name": 7}',  # assigned_name present but not a string
        '{"assigned_name": ""}',  # assigned_name an empty string
    ],
)
def test_rejoin_unusable_register_stdout_degrades_to_requested_name(
    tmp_path: Path, register_stdout: str
) -> None:
    """A recovery register whose stdout carries NO usable assigned name (garbage,
    empty, missing key, non-string, empty string) degrades fail-soft to the
    requested name: the second join goes ``--as <requested>`` and the return
    dict reports ``assigned_name == name``."""

    base = FakeRunner(available=True)

    def runner(argv, **kw):
        cmd = argv[2] if len(argv) > 2 else ""
        if cmd == "join" and not any(c[2] == "register" for c in base.calls):
            base.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, _STALE_EXIT, stdout=_STALE_STDOUT, stderr="")
        if cmd == "register":
            base.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=register_stdout, stderr="")
        return base(argv, **kw)

    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="cycle-1", name="be-1", runner=runner)
    assert result == {
        "transport": "loom",
        "name": "be-1",
        "rejoined": True,
        "reregistered": True,
        "assigned_name": "be-1",
    }
    assert [c[2:] for c in base.calls] == [
        ["join", "cycle-1", "--as", "be-1"],
        ["register", "be-1"],
        ["join", "cycle-1", "--as", "be-1"],
    ]


def test_rejoin_failed_register_degrades_to_requested_name(tmp_path: Path) -> None:
    """A recovery register that FAILS (non-zero exit) never contributes an
    assigned name — even if its error stdout happens to carry one. rejoin
    degrades to the requested name for the second join and the return key."""

    base = FakeRunner(available=True, fail_cmds={"join"})

    def runner(argv, **kw):
        cmd = argv[2] if len(argv) > 2 else ""
        if cmd == "register":
            base.calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, 4, stdout='{"assigned_name": "be-1-9", "error": "boom"}', stderr=""
            )
        return base(argv, **kw)

    status = LoomStatus(available=True, client=_client(tmp_path))
    result = rejoin(status=status, channel="cycle-1", name="be-1", runner=runner)
    assert result["assigned_name"] == "be-1"
    assert result["rejoined"] is False
    # The second join still addressed the requested name, not the error echo.
    assert [c[2:] for c in base.calls][-1] == ["join", "cycle-1", "--as", "be-1"]


# ---------------------------------------------------------------------------
# _run_loom_raw — client-None guard
# ---------------------------------------------------------------------------


def test_run_loom_raw_client_none_guard_no_subprocess(tmp_path: Path) -> None:
    """An available-but-clientless status (LoomStatus(available=True, client=None))
    pins _run_loom_raw's client-None guard: fail-soft ``None`` return and NO
    subprocess ever invoked. Without the guard the runner would be handed
    ``str(None)`` as the client path. Also driven through the production caller
    (rejoin), where a deleted guard would record calls and flip ``rejoined``."""
    runner = FakeRunner(available=True)
    status = LoomStatus(available=True, client=None)

    assert _run_loom_raw(status, ["join", "c", "--as", "be-1"], runner=runner) is None
    assert runner.calls == []

    result = rejoin(status=status, channel="c", name="be-1", runner=runner)
    assert result["rejoined"] is False
    assert runner.calls == []


# ---------------------------------------------------------------------------
# ATELIER_LOOM_COMMS=0 — the single opt-out gate inside detect()
# ---------------------------------------------------------------------------


def test_opt_out_disables_before_any_resolution_or_subprocess(monkeypatch) -> None:
    """ATELIER_LOOM_COMMS=0 short-circuits detect() BEFORE client resolution and
    BEFORE any subprocess: resolve_loom_client is never called and the runner is
    never invoked."""
    import scripts.loom_comms as lc

    def must_not_resolve(env=None):
        raise AssertionError("resolve_loom_client must not be called under opt-out")

    monkeypatch.setattr(lc, "resolve_loom_client", must_not_resolve)
    runner = FakeRunner(available=True)
    status = detect(runner=runner, env={"ATELIER_LOOM_COMMS": "0"})
    assert status.available is False
    assert runner.calls == []


def test_opt_out_applies_even_with_explicit_client(tmp_path: Path) -> None:
    """The opt-out wins even when a resolved client is handed in directly — the
    gate is checked FIRST, so the runner still never fires."""
    runner = FakeRunner(available=True)
    status = detect(client=_client(tmp_path), runner=runner, env={"ATELIER_LOOM_COMMS": "0"})
    assert status.available is False
    assert runner.calls == []


@pytest.mark.parametrize("value", ["1", "false", "no", ""])
def test_opt_out_only_zero_disables(tmp_path: Path, value: str) -> None:
    """ "0" is the ONLY opt-out value — any other value leaves Loom available."""
    runner = FakeRunner(available=True)
    status = detect(client=_client(tmp_path), runner=runner, env={"ATELIER_LOOM_COMMS": value})
    assert status.available is True


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
