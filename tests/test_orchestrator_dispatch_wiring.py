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


# ── model-tier policy flows into the spawn args (atelier model-tier selection) ─
#
# The factory's DEFAULT model_for seam binds scripts.model_tier.recommend to the
# cycle `phase` + the task's `assigned_to` role + optional `difficulty`. These
# pin that the policy's expected tier lands in the enqueued args_json — driving
# the REAL factory, then firing its spawn_fn (the production seam) directly.

import json as _json  # noqa: E402
import sqlite3 as _sqlite3  # noqa: E402

from scripts.model_tier import recommend  # noqa: E402


def _spawn_args(db_path, kind):
    """Return the parsed args_json dicts for all rows of `kind`, FIFO."""
    con = _sqlite3.connect(db_path)
    con.row_factory = _sqlite3.Row
    try:
        rows = con.execute(
            "SELECT args_json FROM bridge_requests WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
        return [_json.loads(r["args_json"]) for r in rows]
    finally:
        con.close()


def test_factory_default_model_for_carries_policy_tier_in_args_json(workspace, monkeypatch):  # noqa: F811
    """A spawned subagent row for a known (phase, role) carries the policy's
    expected tier in args_json — the default model_for seam is wired."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    # review phase + a reviewer role => recommend() = opus (phase opus, floor opus).
    expected = recommend(phase="review", role_id="senior-reviewer-1")
    assert expected == "opus"  # sanity: the policy under test

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        phase="review",
        root=workspace["root"],
    )
    # Fire the production spawn seam directly (no servicer needed — we inspect
    # the enqueued row's args_json, which is what the bridge-poll servicer reads).
    wd._spawn_fn({"id": 1, "assigned_to": "senior-reviewer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert len(args) == 1
    assert args[0]["model"] == "opus"


def test_factory_default_model_for_uses_sonnet_default_no_signal(workspace, monkeypatch):  # noqa: F811
    """LOAD-BEARING: a plain task (unknown phase, non-floored role) gets SONNET,
    not Opus — the cost guarantee flows through the live factory."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        phase=None,  # no cycle-phase signal
        root=workspace["root"],
    )
    wd._spawn_fn({"id": 1, "assigned_to": "backend-engineer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert len(args) == 1
    assert args[0]["model"] == "sonnet"  # NOT opus


def test_factory_default_model_for_haiku_on_mechanical_phase(workspace, monkeypatch):  # noqa: F811
    """A mechanical `doc` phase + a non-floored implementer role => haiku."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        phase="doc",
        root=workspace["root"],
    )
    wd._spawn_fn({"id": 1, "assigned_to": "technical-writer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert args[0]["model"] == "haiku"


def test_factory_env_pin_overrides_policy_via_default_seam(workspace, monkeypatch):  # noqa: F811
    """The ATELIER_MODEL_TIER env pin (operator escape hatch) flows through the
    default seam: a pin forces every spawn to that tier regardless of phase/role."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])
    monkeypatch.setenv("ATELIER_MODEL_TIER", "haiku")

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        phase="review",  # would normally be opus
        root=workspace["root"],
    )
    wd._spawn_fn({"id": 1, "assigned_to": "senior-reviewer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert args[0]["model"] == "haiku"  # env pin wins


def test_factory_injected_model_for_is_honored(workspace, monkeypatch):  # noqa: F811
    """An explicitly injected model_for seam is used verbatim (callers/tests can
    override the default policy)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        model_for=lambda task, attempt: "sonnet",
        phase="review",  # default would be opus; the injected seam overrides
        root=workspace["root"],
    )
    wd._spawn_fn({"id": 1, "assigned_to": "senior-reviewer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert args[0]["model"] == "sonnet"


def test_factory_injected_model_for_none_is_byte_identical(workspace, monkeypatch):  # noqa: F811
    """BACK-COMPAT: injecting `model_for=lambda *a: None` forces session-default
    spawns — args_json has NO "model" key (byte-identical to pre-policy rows)."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        model_for=lambda task, attempt: None,
        phase="review",
        root=workspace["root"],
    )
    wd._spawn_fn({"id": 1, "assigned_to": "senior-reviewer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert "model" not in args[0]


# ── PRODUCTION phase-id END-TO-END through the factory (model-tier review fix #4) ─
#
# The model-tier tests above passed a BARE phase ("review"). A production cycle
# passes the `phases` table id `<base>:<state>` returned by get_phase
# ("review:approved"). This END-TO-END test drives that REAL string through
# build_wave_dispatcher_for_project(phase="review:approved", ...) and asserts the
# spawned bridge_requests row's args_json carries "model":"opus" — proving the
# production phase FORMAT flows the whole way (factory → default model_for →
# normalize_phase → recommend → spawn args_json), not just the bare-key form.


def test_factory_default_model_for_carries_opus_for_real_prod_phase(workspace, monkeypatch):  # noqa: F811
    """A real `<base>:<state>` phase id ("review:approved") drives the default
    model_for seam to opus in the enqueued args_json — the production phase format
    works end to end, not just the bare 'review' key."""
    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    # Sanity: the policy under test resolves the production id to opus.
    assert recommend(phase="review:approved", role_id="senior-reviewer-1") == "opus"

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id="T",
        teammate_name_for=lambda task: str(task["assigned_to"]),
        phase="review:approved",  # the REAL phases-table id, NOT a bare key
        root=workspace["root"],
    )
    wd._spawn_fn({"id": 1, "assigned_to": "senior-reviewer-1"}, 1)

    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert len(args) == 1
    assert args[0]["model"] == "opus"


# ── REAL-ROW e2e: assigned_to survives the DB load/projection (review fix #6) ──
#
# The e2e wiring tests above HAND-BUILD task dicts ({"id": 1, "assigned_to": ...})
# so `assigned_to` is never proven to survive the real tasks-load/projection. This
# case seeds a task via the real `_seed_task` helper (carrying `assigned_to`),
# loads it back through the REAL `scripts.tasks.list_tasks` (a SELECT *), and runs
# the dispatcher on the LOADED row — so a SELECT-projection regression that dropped
# `assigned_to` would make this RED (the model-tier signal would be lost and the
# role-floor would not raise).


def test_factory_model_tier_flows_from_real_loaded_task_row(workspace, monkeypatch):  # noqa: F811
    """A task seeded with `assigned_to`, LOADED via the real tasks helper, flows
    its role through tasks-load → run() → spawn_fn → args_json: a reviewer role on
    a doc phase is raised to opus by the role floor — proving `assigned_to`
    survives the real SELECT projection (not just a hand-built dict)."""
    from scripts import tasks

    monkeypatch.setattr(mode_detector, "detect_mode", lambda: "local")
    persist_dispatch_mode("subagent", root=workspace["root"])

    team_id = "T-REAL"
    recipient = "pm-1"
    _seed_team(workspace, team_id, [recipient])
    # Seed a REAL task row carrying assigned_to (a reviewer role).
    t1 = _seed_task(workspace, title="a", parallel_group=1, assigned_to="senior-reviewer-1")

    # Load it back through the REAL tasks helper (a SELECT * projection).
    loaded = tasks.list_tasks(workspace["db"], project_id=workspace["project_id"])
    assert len(loaded) == 1
    row = loaded[0]
    # The projection MUST carry assigned_to (the regression this test guards).
    assert row["assigned_to"] == "senior-reviewer-1"
    tasks_in = [
        {
            "id": row["id"],
            "assigned_to": row["assigned_to"],
            "parallel_group": 1,
            "created_at": "2026-05-31T00:00:00Z",
        }
    ]

    wd = build_wave_dispatcher_for_project(
        db_path=workspace["db"],
        team_pk="cycle-1",
        team_id=team_id,
        briefing_for=lambda task, attempt: f"B:{task['id']}",
        teammate_name_for=lambda task: recipient,
        # A mechanical `doc` phase base (haiku) — the reviewer ROLE FLOOR raises it
        # to opus, so the assertion is non-vacuous: it depends on assigned_to.
        phase="doc",
        root=workspace["root"],
        sleep_fn=lambda s: time.sleep(0.01),
    )

    with _Servicer(workspace["db"], team_id=team_id, recipient=recipient):
        summaries = wd.run(tasks_in)

    assert summaries[0]["reports"] == {str(t1): "done"}
    args = _spawn_args(workspace["db"], "spawn_subagent")
    assert len(args) == 1
    # doc phase → haiku base, but the senior-REVIEWER floor RAISES to opus.
    # If assigned_to had been dropped by the projection, this would be haiku.
    assert args[0]["model"] == "opus"
