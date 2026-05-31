# tests/test_orchestrator_dispatch_wiring.py
"""Live-wiring of the production dispatch binding into the /atelier:run path
(atelier#85).

#81 (PR #84) shipped ``scripts/atelier_entrypoint.py::build_wave_dispatcher`` —
the production factory that wires the mode-agnostic ``WaveDispatcher`` to the
queue-bridge transport — as a tested-but-DORMANT seam: nothing in a live
``/atelier:run`` orchestrator path constructed it. #85 supplies the missing
orchestrator-entry call site, ``build_wave_dispatcher_for_project``, which:

  * resolves the dispatch mode from the persisted ``.ai/atelier.mode`` marker
    (env override → marker → subagent default — the #62 precedence, unchanged);
  * builds a mode-agnostic ``compose_briefing`` wrapper as the ``briefing_for``
    callable (so the composer stays the single source of prompt text);
  * returns the live ``WaveDispatcher`` bound to ``QueueBridgeDispatchTools`` +
    ``build_spawn_fn`` + ``build_poll_fn`` — exactly the seams #81 wired.

These tests mirror ``tests/test_production_dispatch_e2e.py``'s deterministic
mocked-tool-servicer style: a real Local DB + a background ``_Servicer`` thread
that flips ``bridge_requests`` rows and writes terminal envelopes into
``bridge_messages``. EXACT-COUNT assertions throughout — no loose ``>=``.

The orchestrator TURN-LOOP (per-turn ``bridge_requests`` servicing as a
Claude-followed recipe in ``internal/bridge-poll/SKILL.md``, the live
``/atelier:run`` dispatch-mode UI gate) is procedural and NOT unit-testable
without a live multi-agent harness — the ``_Servicer`` stands in for it here,
and the SKILL wiring is a documented deferral (see the issue blockers).
"""

from __future__ import annotations

import time

from scripts import mode_detector
from scripts.atelier_entrypoint import build_wave_dispatcher_for_project
from scripts.dispatch import persist_dispatch_mode
from scripts.pm_dispatch import WaveDispatcher
from tests.test_production_dispatch_e2e import (  # reuse the e2e harness verbatim
    _count_kind,
    _count_status,
    _seed_task,
    _seed_team,
    _Servicer,
    workspace,  # noqa: F401 — pytest fixture
)


def test_factory_returns_live_wave_dispatcher_subagent(workspace, monkeypatch):  # noqa: F811
    """``build_wave_dispatcher_for_project`` returns a real ``WaveDispatcher``
    with both seams bound, resolving subagent mode from the persisted marker."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T-SUB",
        root=workspace["root"],
    )
    assert isinstance(wd, WaveDispatcher)
    # Both production seams are bound (not the engine's NotImplementedError stubs).
    assert wd._spawn_fn is not WaveDispatcher._unset_spawn
    assert wd._poll_fn is not WaveDispatcher._unset_poll
    # No explicit escalate_fn passed → the engine's guaranteed-emitting default.
    assert wd._escalate_fn is not None


def test_factory_drives_subagent_wave_to_completion(workspace, monkeypatch):  # noqa: F811
    """End-to-end: the factory-built dispatcher drives a subagent-mode wave to
    terminal-only closure through the queue-bridge transport + mocked servicer.

    This is the live-invocation #85 adds — the same barrier semantics as the
    #81 e2e, but reached via the orchestrator-entry factory rather than
    hand-wired seams."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    team_id = "T-SUB"
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient])
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    t2 = _seed_task(workspace, title="b", parallel_group=1)
    tasks = [
        {"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"},
        {"id": t2, "parallel_group": 1, "created_at": "2026-05-31T00:00:01Z"},
    ]

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id=team_id,
        briefing_for=lambda task, attempt: f"B:{task['id']}:{attempt}",
        teammate_name_for=lambda task: recipient,
        root=workspace["root"],
        sleep_fn=lambda s: time.sleep(0.01),
    )

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        summaries = wd.run(tasks)

    assert len(summaries) == 1
    assert summaries[0]["terminal_only"] is True
    assert summaries[0]["reports"] == {str(t1): "done", str(t2): "done"}
    # EXACT-COUNT: one spawn_subagent per task, no team kinds; all serviced ready.
    assert _count_kind(workspace["db"], "spawn_subagent") == 2
    assert _count_kind(workspace["db"], "create_team") == 0
    assert _count_status(workspace["db"], "ready") == 2
    assert _count_status(workspace["db"], "pending") == 0


def test_factory_drives_agent_team_wave_create_team_once(workspace, monkeypatch):  # noqa: F811
    """agent-team mode resolved from the marker: create_team fires EXACTLY once
    across the whole wave, then per-task first-touch spawn_teammate."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("agent-team", root=workspace["root"])

    team_id = "T-TEAM"
    recipient = "pm-1"
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    t2 = _seed_task(workspace, title="b", parallel_group=1)
    _seed_team(workspace, team_id, [recipient, str(t1), str(t2)])
    tasks = [
        {"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"},
        {"id": t2, "parallel_group": 1, "created_at": "2026-05-31T00:00:01Z"},
    ]

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id=team_id,
        briefing_for=lambda task, attempt: f"B:{task['id']}:{attempt}",
        members=[str(t1), str(t2)],
        team_name="cycle-team",
        teammate_name_for=lambda task: str(task["id"]),
        role_id_for=lambda task: recipient,
        teams_root=workspace["root"] / "no-such-teams-root",  # force first-touch
        root=workspace["root"],
        sleep_fn=lambda s: time.sleep(0.01),
    )

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        summaries = wd.run(tasks)

    assert summaries[0]["terminal_only"] is True
    assert summaries[0]["reports"] == {str(t1): "done", str(t2): "done"}
    # EXACT-COUNT: create_team exactly once; first-touch spawn_teammate per task.
    assert _count_kind(workspace["db"], "create_team") == 1
    assert _count_kind(workspace["db"], "spawn_teammate") == 2
    assert _count_kind(workspace["db"], "send_message") == 0


def test_factory_threads_escalate_fn_through(workspace, monkeypatch):  # noqa: F811
    """A caller-supplied ``escalate_fn`` is threaded straight through to the
    engine (the #87 persona-gap escalation seam binds here)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    sentinel = []

    def escalate_fn(escalation):
        sentinel.append(escalation)

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T-SUB",
        escalate_fn=escalate_fn,
        root=workspace["root"],
    )
    assert wd._escalate_fn is escalate_fn


def test_factory_env_override_beats_marker(workspace, monkeypatch):  # noqa: F811
    """The ``ATELIER_DISPATCH_MODE`` env override wins over the marker (#62
    precedence): marker says subagent, env says agent-team → agent-team is
    selected (so create_team would fire)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    team_id = "T-ENV"
    recipient = "pm-1"
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    _seed_team(workspace, team_id, [recipient, str(t1)])
    tasks = [{"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"}]

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id=team_id,
        briefing_for=lambda task, attempt: "B",
        members=[str(t1)],
        team_name="cycle-team",
        teammate_name_for=lambda task: str(task["id"]),
        role_id_for=lambda task: recipient,
        teams_root=workspace["root"] / "no-such-teams-root",
        env={"ATELIER_DISPATCH_MODE": "agent-team"},
        root=workspace["root"],
        sleep_fn=lambda s: time.sleep(0.01),
    )

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        summaries = wd.run(tasks)

    assert summaries[0]["reports"] == {str(t1): "done"}
    assert _count_kind(workspace["db"], "create_team") == 1


def test_factory_default_briefing_for_is_safe(workspace, monkeypatch):  # noqa: F811
    """When no ``briefing_for`` is supplied the factory falls back to a
    deterministic per-task stub (never crashes for lack of a composer wrapper);
    a real run threads a ``compose_briefing`` wrapper, but the default keeps the
    dispatcher constructible from a minimal call site."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    team_id = "T-DEF"
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient])
    t1 = _seed_task(workspace, title="a", parallel_group=1)
    tasks = [{"id": t1, "parallel_group": 1, "created_at": "2026-05-31T00:00:00Z"}]

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id=team_id,
        teammate_name_for=lambda task: recipient,
        root=workspace["root"],
        sleep_fn=lambda s: time.sleep(0.01),
    )

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        summaries = wd.run(tasks)
    assert summaries[0]["reports"] == {str(t1): "done"}
