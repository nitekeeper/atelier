"""M6a — the FIRST production CLI/host execution path (Iron-Law tests).

Covers the four M6a deliverables:

* T1 — :func:`scripts.cli_dispatch.build_cli_dispatch_for_project` (the
  recommend-backed CLI dispatch factory), proven via the production caller.
* T2 — :func:`scripts.host_scheduler.run_host_pipeline_for_project` (the first
  production caller of ``pipeline()``) + the ``ATELIER_TRANSPORT`` routing seam
  in ``scripts.dispatch`` (:func:`is_host_transport` / :func:`dispatch_host_pipeline`).
* T3 — :mod:`scripts.roster` in-memory persona resolution (DB-free).
* T6 — Loom observability-only on the host path (byte-identical with/without).

ALL tests run against :class:`scripts.cli_dispatch.FakeCliRunner` — NO real
``claude`` is ever spawned (and FakeCliRunner is exempt from the mandatory-sandbox
gate). The argv is captured per call (``runner.calls[i]["argv"]``) and the
``--model`` value is read with the ``test_cli_dispatch.py`` ``val('--model')``
pattern — so the model-tier assertions are BEHAVIORAL (real argv), not mocked.
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    FakeCliRunner,
    build_cli_dispatch_for_project,
    is_failed_attempt,
    max_budget_usd_for,
)
from scripts.cli_dispatch import (
    _default_est_for as _cli_default_est_for,
)
from scripts.dispatch import (
    TRANSPORT_BRIDGE,
    TRANSPORT_CLI,
    UnknownTransportError,
    dispatch_host_pipeline,
    is_host_transport,
    resolve_transport,
)
from scripts.host_scheduler import run_host_pipeline_for_project
from scripts.roster import (
    ROSTER_BY_PERSONA,
    PersonaNotInRosterError,
    resolve_persona,
)
from scripts.seed_roles import ROLES

# Parse the dispatched ``task T<id> (attempt <n>)`` out of the ``-p`` prompt so the
# fake echoes the right task_id/attempt (validate_envelope is anti-spoof).
_PROMPT_RE = re.compile(r"task (\S+) \(attempt (\d+)\)")


def _val(argv, flag):
    """Return the value following ``flag`` in ``argv`` (the test_cli_dispatch pattern)."""
    return argv[argv.index(flag) + 1]


def _echo_envelope(argv, cwd):
    """Per-task structured_output: echo the dispatched task_id/attempt from the -p
    prompt so validate_envelope (anti-spoof) accepts the reply for ANY task."""
    prompt = _val(argv, "-p")
    m = _PROMPT_RE.search(prompt)
    task_id, attempt = m.group(1), int(m.group(2))
    return {
        "type": "task_result",
        "task_id": task_id,
        "attempt": attempt,
        "status": "done",
        "artifacts": [{"path": "f.py", "sha": "s"}],
        "notes_md": "ok",
    }


def _argv_for_task(runner, task_id):
    """Return the captured argv whose -p prompt names ``task_id`` (exactly one)."""
    matches = [
        c["argv"]
        for c in runner.calls
        if (m := _PROMPT_RE.search(_val(c["argv"], "-p"))) and m.group(1) == task_id
    ]
    assert len(matches) == 1, f"expected exactly one dispatch for {task_id}, got {len(matches)}"
    return matches[0]


def _two_task_dag():
    """A 2-task, same-wave, write-disjoint DAG (no writes → no worktree needed):

    * a non-floored ``doc``-phase task with a plain persona → PHASE_TIER doc =
      ``haiku``;
    * a security-role task ALSO at ``phase='doc'`` → its opus comes
      UNAMBIGUOUSLY from ROLE_FLOOR (the doc phase alone would pick haiku), so the
      two-distinct-tiers claim rests on TWO GENUINELY DIFFERENT mechanisms
      (PHASE_TIER vs ROLE_FLOOR), not one over-determined opus (FIX 5).

    Two DIFFERENT tiers from the SAME ``recommend`` policy — impossible to satisfy
    with a constant ``model_for``. No ``writes`` so the tasks need no git worktree
    isolation; same ``parallel_group`` and write-disjoint → both barrier-free.
    """
    return [
        {
            "task_id": "DOC",
            "parallel_group": 0,
            "assigned_persona": "backend-engineer-1",
            "phase": "doc",
            "description": "write a doc",
        },
        {
            "task_id": "SEC",
            "parallel_group": 0,
            "assigned_persona": "security-engineer-1",
            # phase='doc' (a NON-opus phase) so SEC's opus is UNAMBIGUOUSLY the
            # ROLE_FLOOR raising it — not the phase. (FIX 5: isolate the floor.)
            "phase": "doc",
            "description": "review the security posture",
        },
    ]


def _run_host(tasks, runner, tmp_path, *, env=None, budget_tokens=10_000_000, clone_dir=None):
    """Drive the M6a production host entrypoint with a FakeCliRunner (no clone
    needed — tasks have no writes, so worktree_factory stays None).

    ``clone_dir`` may be pinned so two runs share the SAME clone path (the
    byte-identical Loom test needs the clone path — which rides ``--add-dir`` — to
    be the only constant, not a per-run-distinct tmp dir)."""
    from scripts.result_journal import ResultJournal

    if clone_dir is None:
        clone = tmp_path / "experiment" / "o-r"
        clone.mkdir(parents=True, exist_ok=True)
        clone_dir = str(clone)
    budget = BudgetPool(total_tokens=budget_tokens)
    journal = ResultJournal()
    return asyncio.run(
        run_host_pipeline_for_project(
            tasks,
            clone_dir=clone_dir,
            budget=budget,
            journal=journal,
            runner=runner,
            env=env if env is not None else {},
        )
    )


# ── Iron-Law 1: per-call model_tier flows (TWO distinct --model values) ──────


def test_cli_host_dispatch_emits_model_tier_per_call(tmp_path):
    """Drive the NEW CLI factory/entrypoint with a 2-task DAG; capture real argv;
    assert the doc task's --model == 'haiku' (PHASE_TIER doc) and the security-role
    task's --model == 'opus' — coming from TWO GENUINELY DIFFERENT mechanisms
    (PHASE_TIER vs ROLE_FLOOR), since BOTH tasks are phase='doc' so SEC's opus is
    purely the role floor (FIX 5). TWO DISTINCT tiers from recommend —
    un-satisfiable by a constant model_for.

    Also locks the per-tier BUDGET COMPOUNDING through the real host wiring: the
    --max-budget-usd argv tracks the chosen tier (a wrong tier sets BOTH --model
    AND the budget ceiling), so haiku's ceiling < opus's ceiling (FIX 6)."""
    runner = FakeCliRunner(structured_output=_echo_envelope)
    results = _run_host(_two_task_dag(), runner, tmp_path)

    # Both tasks completed (no failed attempts) — the per-task tier + briefing flow
    # actually ran end to end, not just constructed.
    assert all(not is_failed_attempt(r) for r in results), results
    assert {r["task_id"] for r in results} == {"DOC", "SEC"}

    doc_argv = _argv_for_task(runner, "DOC")
    sec_argv = _argv_for_task(runner, "SEC")

    # DOC: doc phase, non-floored persona → PHASE_TIER doc = haiku.
    assert _val(doc_argv, "--model") == "haiku", (
        "doc-phase non-floored task must be PHASE_TIER haiku"
    )
    # SEC: SAME doc phase (would be haiku) — the security ROLE_FLOOR alone raises it.
    assert _val(sec_argv, "--model") == "opus", (
        "security role must be ROLE_FLOOR opus (phase is doc)"
    )
    # The load-bearing claim: TWO DISTINCT tiers from one recommend-backed policy,
    # via two distinct mechanisms (phase vs role floor).
    assert _val(doc_argv, "--model") != _val(sec_argv, "--model")

    # FIX 6 — per-tier budget compounding through the REAL host wiring. The
    # --max-budget-usd ceiling derives from est_for(tier), so the cheaper tier
    # carries a strictly smaller ceiling. Pin the concrete values so a constant
    # est_for (mutation L) cannot satisfy both.
    haiku_budget = float(_val(doc_argv, "--max-budget-usd"))
    opus_budget = float(_val(sec_argv, "--max-budget-usd"))
    assert haiku_budget < opus_budget, "per-tier budget ceiling must track the tier"
    assert (
        _val(doc_argv, "--max-budget-usd")
        == f"{max_budget_usd_for(_cli_default_est_for('haiku')):.2f}"
    )
    assert (
        _val(sec_argv, "--max-budget-usd")
        == f"{max_budget_usd_for(_cli_default_est_for('opus')):.2f}"
    )


def test_role_floor_opus_is_a_hard_floor_on_host_path(tmp_path):
    """A review/security/architect/safety role floors to opus EVEN when its phase
    would otherwise pick a cheaper tier (here phase='doc' → haiku, role raises it).
    Proves the ROLE_FLOOR is intact on the host path (not weakened)."""
    floored = [
        {
            "task_id": "ARCH",
            "parallel_group": 0,
            "assigned_persona": "software-architect-1",
            "phase": "doc",  # cheap phase — the floor must override it
            "description": "x",
        }
    ]
    runner = FakeCliRunner(structured_output=_echo_envelope)
    _run_host(floored, runner, tmp_path)
    assert _val(_argv_for_task(runner, "ARCH"), "--model") == "opus"


def test_env_model_tier_pin_honored_on_host_path(tmp_path):
    """ATELIER_MODEL_TIER pins ALL tasks (operator global escape hatch) — proving
    the host model_for reuses the SAME precedence as the bridge.

    The env pin is precedence rung 2 in ``model_tier.recommend`` and RETURNS
    OUTRIGHT (above difficulty/phase AND above the rung-4 ROLE_FLOOR), exactly as
    the bridge ``_default_model_for`` does. So with the pin set, EVERY task —
    including the security role — renders the pinned tier. This is the bridge-
    identical behavior we are reusing, NOT a host-only quirk."""
    runner = FakeCliRunner(structured_output=_echo_envelope)
    _run_host(_two_task_dag(), runner, tmp_path, env={"ATELIER_MODEL_TIER": "sonnet"})
    # Both the doc-phase task (would be haiku) AND the security-role task (would be
    # opus via floor) collapse to the pin — the pin wins outright (rung 2 returns).
    assert _val(_argv_for_task(runner, "DOC"), "--model") == "sonnet"
    assert _val(_argv_for_task(runner, "SEC"), "--model") == "sonnet"


def test_env_pin_matches_bridge_default_model_for(tmp_path):
    """Cross-check: the host model_for and the bridge `_default_model_for` return
    the SAME tier for the same (phase, persona, env) — proving the host path REUSES
    the bridge policy, not a parallel re-implementation."""
    from scripts.atelier_entrypoint import _default_model_for
    from scripts.cli_dispatch import _host_model_for

    env = {"ATELIER_MODEL_TIER": "sonnet"}
    host_mf = _host_model_for(env)
    sec_task = {"task_id": "SEC", "phase": "review", "assigned_persona": "security-engineer-1"}
    # The bridge closure closes over the cycle phase and reads `assigned_to`.
    bridge_mf = _default_model_for("review", env)
    bridge_view = {"task_id": "SEC", "assigned_to": "security-engineer-1"}
    assert host_mf(sec_task, 1) == bridge_mf(bridge_view, 1) == "sonnet"

    # And without a pin: the floor flows identically (both opus).
    host_mf2 = _host_model_for({})
    bridge_mf2 = _default_model_for("review", {})
    assert host_mf2(sec_task, 1) == bridge_mf2(bridge_view, 1) == "opus"


# ── Iron-Law 2: in-memory roster resolves persona profile WITHOUT a DB ───────


def test_roster_in_memory_resolves_persona_profile_without_db(tmp_path):
    """With NO agents.db anywhere, the CLI factory resolves a known
    assigned_persona to its agent_profile text from the in-memory roster, and the
    composed briefing (--system-prompt) contains that body verbatim."""
    # The seed's canonical profile body for this persona (single source of truth).
    seed_profile = next(r["agent_profile"] for r in ROLES if r["agent_id"] == "backend-engineer-1")
    # Sanity: resolve_persona returns exactly the seed body, no DB involved.
    assert resolve_persona("backend-engineer-1") == seed_profile
    assert ROSTER_BY_PERSONA["backend-engineer-1"] == seed_profile

    task = [
        {
            "task_id": "DOC",
            "parallel_group": 0,
            "assigned_persona": "backend-engineer-1",
            "phase": "doc",
            "description": "write a doc",
        }
    ]
    runner = FakeCliRunner(structured_output=_echo_envelope)
    # tmp_path holds NO atelier.db — the resolution is purely in-memory.
    _run_host(task, runner, tmp_path)

    briefing = _val(_argv_for_task(runner, "DOC"), "--system-prompt")
    # The persona body is fed into compose_briefing(persona_profile_text=...) and
    # appears in the rendered briefing's "YOUR PERSONA" section. The Jinja2 template
    # HTML-escapes the body on render (e.g. `'` → `&#39;`), so we assert distinctive
    # apostrophe-free phrases from THIS persona's seed body are present (proving the
    # right persona — not a placeholder, not another role — was resolved in-memory).
    assert "YOUR PERSONA" in briefing
    assert "PhD in Computer Science, MIT. 24 years building production backend" in briefing
    assert "Pragmatic and evidence-based" in briefing
    # Anti-confusion: a DIFFERENT role's distinctive phrase must NOT appear.
    assert "PhD in Organizational Psychology" not in briefing  # that is the PM persona
    # And the CLI-transport addendum is present (host path → structured return).
    assert "TRANSPORT OVERRIDE — CLI MODE" in briefing


def test_roster_unknown_persona_fails_loud():
    """An unknown persona is a planner DEFECT — resolve_persona raises (fail-loud),
    never silently substitutes a placeholder body."""
    with pytest.raises(PersonaNotInRosterError):
        resolve_persona("not-a-real-role-99")
    with pytest.raises(PersonaNotInRosterError):
        resolve_persona(None)


def test_roster_keyed_on_real_planner_field_agent_id():
    """The roster is keyed on the REAL planner field value: the agent_id that the
    planner emits as `assigned_persona` (e.g. 'backend-engineer-1'), NOT a slug or
    role_name. Every seed agent_id resolves; a role_name does NOT."""
    for role in ROLES:
        assert resolve_persona(role["agent_id"]) == role["agent_profile"]
    # The role_name ("Software Engineer (Backend)") is NOT a roster key — the
    # planner emits the agent_id form, which is what we key on.
    with pytest.raises(PersonaNotInRosterError):
        resolve_persona("Software Engineer (Backend)")


# ── Iron-Law 3: Loom is observability-only on the host path (byte-identical) ──


def _capture_run(clone_dir):
    """Run the host dispatch once (over a FIXED clone dir) and return
    (argv-list, dispatch-order, results) so two runs can be compared byte-for-byte.
    The clone dir is pinned so the only variable across the two runs is the AMBIENT
    Loom env the caller sets via monkeypatch (otherwise the per-run tmp clone path
    would ride --add-dir and diverge). ``env`` is intentionally left to the host
    default (``os.environ``) so the REAL Loom-detection env read is exercised."""
    runner = FakeCliRunner(structured_output=_echo_envelope)
    # env=None → run_host_pipeline_for_project falls back to os.environ, the SAME
    # mapping scripts.loom_comms.detect / the opt-out gate read. So toggling
    # os.environ["ATELIER_LOOM_COMMS"] exercises the real Loom path, not a stub arg.
    results = _run_host(_two_task_dag(), runner, None, env=None, clone_dir=clone_dir)
    argvs = [c["argv"] for c in runner.calls]
    order = [_PROMPT_RE.search(_val(c["argv"], "-p")).group(1) for c in runner.calls]
    return argvs, order, results


def _env_honoring_detect_stub(monkeypatch, tmp_path):
    """Install a loom_comms.detect stub that HONORS ATELIER_LOOM_COMMS (FIX 2).

    A naive ``lambda: LoomStatus(available=True)`` stub is DEAD two ways: (a) it
    leaves ``client=None`` so ``build_team_chat_context`` ALWAYS collapses to
    bridge (its gate is ``available AND client is not None``), and (b) because it
    fully replaces ``detect``, the real ``ATELIER_LOOM_COMMS=0`` opt-out gate
    (which lives INSIDE the real ``detect``) never runs, so on/off return the same
    status. This stub fixes BOTH: it reads the env opt-out itself and returns an
    available status WITH a non-None client path when Loom is permitted, else an
    unavailable status. So toggling the env genuinely changes resolved availability
    — a realistic detect-driven leak would then DIVERGE."""
    import scripts.loom_comms as loom_comms

    fake_client = tmp_path / "loom_chat.py"
    fake_client.write_text("# stub client\n")

    def _detect(*_a, env=None, **_k):
        envmap = env if env is not None else os.environ
        if envmap.get(loom_comms.LOOM_COMMS_ENV_VAR) == "0":
            return loom_comms.LoomStatus(available=False)
        return loom_comms.LoomStatus(
            available=True,
            url="http://127.0.0.1:7077/mcp",
            port=7077,
            source="stub",
            client=fake_client,
        )

    monkeypatch.setattr(loom_comms, "detect", _detect, raising=False)
    return loom_comms


def test_loom_off_vs_on_host_dispatch_byte_identical(tmp_path, monkeypatch):
    """Run the CLI host dispatch twice over the same DAG and the SAME clone: once
    with Loom genuinely AVAILABLE (env permits + stub carries a non-None client),
    once with ATELIER_LOOM_COMMS=0 (the documented opt-out). The detect stub HONORS
    the env opt-out (FIX 2), so the on/off toggle actually changes resolved
    availability — a detect-driven leak (mutation J) WOULD diverge.

    Assert captured argv lists, dispatch order, and result envelopes are IDENTICAL
    — proving Loom carries NO control-plane decision on the host path (it is
    observability-only). The os.environ direct-env-read path (mutation K) stays
    covered too: the env is toggled on os.environ, the same mapping the host falls
    back to."""
    clone = tmp_path / "experiment" / "o-r"
    clone.mkdir(parents=True)
    clone_dir = str(clone)

    # The env-honoring detect stub (FIX 2) — carries a non-None client when Loom is
    # permitted so build_team_chat_context would emit a loom branch IF the host path
    # consulted it; honors the opt-out so on/off genuinely differ.
    _env_honoring_detect_stub(monkeypatch, tmp_path)

    # Run A: Loom AVAILABLE (env does not carry the opt-out).
    monkeypatch.delenv("ATELIER_LOOM_COMMS", raising=False)
    argvs_on, order_on, results_on = _capture_run(clone_dir)

    # Run B: Loom OFF via the documented opt-out in the AMBIENT os.environ.
    monkeypatch.setenv("ATELIER_LOOM_COMMS", "0")
    argvs_off, order_off, results_off = _capture_run(clone_dir)

    assert order_on == order_off, "dispatch order diverged with Loom toggled"
    assert argvs_on == argvs_off, "captured argv diverged with Loom toggled"
    # Result envelopes (sans the engine's incidental ordering) are identical.
    by_on = {r["task_id"]: r for r in results_on}
    by_off = {r["task_id"]: r for r in results_off}
    assert by_on == by_off, "result envelopes diverged with Loom toggled"

    # FIX 3 — POSITIVE structural backstop (replaces the old tautology). The host
    # briefing must be a BRIDGE briefing, NOT a Loom one: the Loom chat-protocol
    # markers (only rendered when team_chat.transport == 'loom') MUST be ABSENT,
    # and the bridge CHANNELS fallback MUST be present. This FAILS if a Loom
    # team_chat ever leaked into the host briefing (proven able-to-fail below).
    sys_prompt = _val(argvs_on[0], "--system-prompt")
    for loom_marker in _LOOM_BRIEFING_MARKERS:
        assert loom_marker not in sys_prompt, (
            f"Loom marker leaked into host briefing: {loom_marker!r}"
        )
    assert "bridge_send" in sys_prompt, "bridge CHANNELS fallback missing from host briefing"


#: Loom-only briefing markers — these render ONLY when ``team_chat.transport ==
#: 'loom'`` (role.j2). Their ABSENCE distinguishes a bridge briefing from a loom
#: briefing; their presence would mean a loom team_chat leaked in (FIX 3).
_LOOM_BRIEFING_MARKERS = (
    "## Loom team-chat (PEER chat",
    "PEER chat routes through the **Loom channel",
    "| Register (as",
)


def test_loom_structural_backstop_can_fail_on_a_leaked_loom_briefing(tmp_path):
    """FIX 3 proof — the positive backstop is NOT a tautology: a briefing composed
    WITH a loom team_chat DOES contain the loom markers, so the absence-assertion
    would fail on a leak. (Distinguishes a bridge briefing from a loom one.)"""
    from scripts.dispatch import compose_briefing
    from scripts.loom_comms import LoomStatus, build_team_chat_context

    fake_client = tmp_path / "loom_chat.py"
    fake_client.write_text("# stub\n")
    loom_chat = build_team_chat_context(
        LoomStatus(available=True, url="u", port=1, source="s", client=fake_client),
        role_id="backend-engineer-1",
        channel="atelier-team",
        team_lead_name="team-lead",
    )
    assert loom_chat["transport"] == "loom"
    leaked = compose_briefing(
        role_id="backend-engineer-1",
        task_id="DOC",
        persona_profile_text="P",
        phase_procedure_text="PP",
        task_brief="TB",
        team_id="t",
        team_lead_name="team-lead",
        wave_id="w",
        wave_phase="doc",
        deadline_iso="2099-01-01T00:00:00Z",
        team_chat=loom_chat,
        transport=TRANSPORT_CLI,
    )
    # At least one loom marker IS present in a leaked-loom briefing → the backstop's
    # absence-assertion would FAIL on it (it is not vacuously true).
    assert any(m in leaked for m in _LOOM_BRIEFING_MARKERS), (
        "a loom team_chat must render loom markers — else the backstop is vacuous"
    )


# ── Default-transport guard: unset/default still selects BRIDGE ──────────────


def test_default_transport_is_bridge_no_host_routing():
    """ATELIER_TRANSPORT unset / empty / whitespace resolves to bridge, and
    is_host_transport is False — the default path is NOT routed through the host
    pipeline (no behavior change to the default)."""
    assert resolve_transport(env={}) == TRANSPORT_BRIDGE
    assert resolve_transport(env={"ATELIER_TRANSPORT": ""}) == TRANSPORT_BRIDGE
    assert resolve_transport(env={"ATELIER_TRANSPORT": "  "}) == TRANSPORT_BRIDGE
    assert is_host_transport(env={}) is False
    assert is_host_transport(env={"ATELIER_TRANSPORT": ""}) is False


def test_cli_transport_routes_to_host():
    """ATELIER_TRANSPORT=cli selects the host path (is_host_transport True)."""
    assert resolve_transport(env={"ATELIER_TRANSPORT": "cli"}) == TRANSPORT_CLI
    assert is_host_transport(env={"ATELIER_TRANSPORT": "cli"}) is True


def test_dispatch_host_pipeline_routes_through_real_pipeline(tmp_path):
    """FIX 1 — the dispatch_host_pipeline ROUTING SEAM has real coverage: when
    ATELIER_TRANSPORT=cli, dispatch_host_pipeline(...) must drive the SAME host
    pipeline as a direct run_host_pipeline_for_project over the same task set, and
    return EQUAL envelopes. (Gutting dispatch_host_pipeline to `return []` must
    make this RED — it does not just forward, it must actually dispatch.)

    Also asserts the default (env={}) does NOT select this path (is_host_transport
    False) — the bridge/SKILL never reaches dispatch_host_pipeline."""
    # Routing predicate: cli → host path; default → NOT this path.
    assert is_host_transport(env={"ATELIER_TRANSPORT": "cli"}) is True
    assert is_host_transport(env={}) is False

    from scripts.result_journal import ResultJournal

    tasks = _two_task_dag()

    # Reference: a DIRECT run over the same DAG (the path FIX-1 routes to).
    ref_clone = tmp_path / "ref" / "experiment" / "o-r"
    ref_clone.mkdir(parents=True)
    ref = asyncio.run(
        run_host_pipeline_for_project(
            tasks,
            clone_dir=str(ref_clone),
            budget=BudgetPool(total_tokens=10_000_000),
            journal=ResultJournal(),
            runner=FakeCliRunner(structured_output=_echo_envelope),
            env={},
        )
    )

    # Through the routing seam: clone_dir/budget/journal are keyword-only;
    # runner/env are forwarded via **kwargs to run_host_pipeline_for_project.
    seam_clone = tmp_path / "seam" / "experiment" / "o-r"
    seam_clone.mkdir(parents=True)
    routed = asyncio.run(
        dispatch_host_pipeline(
            tasks,
            clone_dir=str(seam_clone),
            budget=BudgetPool(total_tokens=10_000_000),
            journal=ResultJournal(),
            runner=FakeCliRunner(structured_output=_echo_envelope),
            env={},
        )
    )

    # The seam genuinely dispatched (non-empty) and returned envelopes EQUAL to the
    # direct path (key by task_id — order is deterministic but compare by id to be
    # robust). A gutted `return []` seam fails the non-empty + equality asserts.
    assert routed, "dispatch_host_pipeline returned nothing — it did not dispatch"
    assert {r["task_id"] for r in routed} == {"DOC", "SEC"}
    assert {r["task_id"]: r for r in routed} == {r["task_id"]: r for r in ref}


def test_unknown_transport_fails_loud():
    """A typo'd transport is fail-loud — it must NOT silently select a default."""
    with pytest.raises(UnknownTransportError):
        is_host_transport(env={"ATELIER_TRANSPORT": "grpc"})


# ── T1 factory: constructs a real CliDispatchTools with the recommend default ─


def test_factory_builds_tools_with_recommend_backed_model_for(tmp_path):
    """build_cli_dispatch_for_project returns a CliDispatchTools whose default
    model_for is the recommend-backed per-task seam (doc→haiku, security→opus)."""
    from scripts.result_journal import ResultJournal

    clone = tmp_path / "clone"
    clone.mkdir()
    with build_cli_dispatch_for_project(
        clone_dir=str(clone),
        budget=BudgetPool(total_tokens=1_000_000),
        journal=ResultJournal(),
        runner=FakeCliRunner(structured_output=_echo_envelope),
        env={},
    ) as tools:
        doc = {"task_id": "DOC", "phase": "doc", "assigned_persona": "backend-engineer-1"}
        sec = {"task_id": "SEC", "phase": "review", "assigned_persona": "security-engineer-1"}
        assert tools.model_for(doc, 1) == "haiku"
        assert tools.model_for(sec, 1) == "opus"
        # The briefing seam resolves the persona from the in-memory roster (the
        # body is rendered HTML-escaped by the template, so assert a distinctive
        # apostrophe-free phrase from THIS persona's seed body).
        brief = tools.briefing_for(doc, 1)
        assert "PhD in Computer Science, MIT. 24 years building production backend" in brief


def test_factory_none_run_mode_posture_matches_host_entrypoint(tmp_path):
    """FIX 5 — sibling None-divergence closed: build_cli_dispatch_for_project with
    run_mode=None auto-resolves the posture the SAME way the host entrypoint does
    (resolve_run_mode → saved-profile default = cost-lean), so the SAME None yields
    the SAME posture across both sibling constructors — no transport-shape drift.

    Probe: a qa task (base sonnet) renders haiku under BOTH the factory's default-None
    model_for AND the host entrypoint's default-None path. A pre-FIX-5 factory (which
    treated None as neutral) would have left it sonnet — mismatching the host's
    cost-lean haiku."""
    from scripts.result_journal import ResultJournal
    from scripts.run_mode import resolve_run_mode

    env = {}  # no ATELIER_RUN_MODE pin → saved-profile default = cost-lean
    saved = resolve_run_mode(env=env)
    assert saved.mode_id == "cost-lean"  # the fixed saved default

    qa = {
        "task_id": "QA",
        "parallel_group": 0,
        "phase": "qa",
        "assigned_persona": "backend-engineer-1",
    }
    # Factory: run_mode OMITTED (None) → auto-resolves cost-lean posture.
    clone = tmp_path / "clone"
    clone.mkdir()
    with build_cli_dispatch_for_project(
        clone_dir=str(clone),
        budget=BudgetPool(total_tokens=1_000_000),
        journal=ResultJournal(),
        runner=FakeCliRunner(structured_output=_echo_envelope),
        env=env,
    ) as tools:
        factory_tier = tools.model_for(qa, 1)

    # Host entrypoint: run_mode OMITTED (None) → same cost-lean posture, observed via argv.
    host_runner = _ConcurrencyRunner()
    clone2 = tmp_path / "host"
    clone2.mkdir()
    asyncio.run(
        run_host_pipeline_for_project(
            [qa],
            clone_dir=str(clone2),
            budget=BudgetPool(total_tokens=1_000_000),
            journal=ResultJournal(),
            runner=host_runner,
            env=env,
            max_workers=5,
        )
    )
    host_tier = _val(_argv_for_task(host_runner, "QA"), "--model")

    # PARITY: both default-None siblings produce the cost-lean-biased haiku (NOT the
    # neutral sonnet a pre-FIX-5 factory would have left).
    assert factory_tier == host_tier == "haiku"


# ── M6b-2 R-MODE: the four levers fan out from run_host_pipeline_for_project ──


class _ConcurrencyRunner(FakeCliRunner):
    """FakeCliRunner that records max in-flight concurrency (for the fleet lever)
    and echoes a valid envelope. Inherits the FAIL-CLOSED fake markers (no real
    process → sandbox-gate exempt)."""

    def __init__(self):
        super().__init__(structured_output=_echo_envelope)
        self._live = 0
        self.max_concurrency = 0
        self._lock = asyncio.Lock()

    async def __call__(self, argv, cwd):
        async with self._lock:
            self._live += 1
            self.max_concurrency = max(self.max_concurrency, self._live)
        await asyncio.sleep(0.05)
        try:
            return await super().__call__(argv, cwd)
        finally:
            async with self._lock:
                self._live -= 1


def _run_host_mode(tasks, runner, tmp_path, *, run_mode, budget_tokens, max_workers=5):
    """Drive the host entrypoint with an explicit run_mode + budget, returning
    (results, runner) so the caller can inspect argv + concurrency."""
    from scripts.result_journal import ResultJournal

    clone = tmp_path / "experiment" / "o-r"
    clone.mkdir(parents=True, exist_ok=True)
    asyncio.run(
        run_host_pipeline_for_project(
            tasks,
            clone_dir=str(clone),
            budget=BudgetPool(total_tokens=budget_tokens),
            journal=ResultJournal(),
            runner=runner,
            env={"CI": "1"},  # non-interactive: no prompt, no block
            run_mode=run_mode,
            max_workers=max_workers,
        )
    )
    return runner


def _disjoint_tasks(n):
    """n same-wave, write-disjoint qa-phase tasks (so they CAN run concurrently —
    the fleet lever is what limits them)."""
    return [
        {
            "task_id": f"t{i}",
            "parallel_group": 0,
            "writes": [f"f{i}"],
            "assigned_persona": "backend-engineer-1",
            "phase": "qa",
        }
        for i in range(n)
    ]


def test_run_mode_threads_budget_and_fleet(tmp_path):
    """The THREE modes produce DIFFERENT budget/fleet numbers through the REAL
    entrypoint: cost-lean → narrower static_fleet_width (tighter pool + smaller
    max_workers cap) than balanced than quality-lean. Asserts DISTINCT observed
    concurrency across the three, AND distinct posture-driven --model tiers.

    RED pre-fix: run_host_pipeline_for_project has no ``run_mode`` kwarg →
    TypeError, so the test is RED until the kwarg + budget/fleet fan-out exist."""
    from scripts.run_mode import resolve_run_mode

    ci = {"CI": "1"}
    cost = resolve_run_mode(explicit="cost-lean", env=ci)
    balanced = resolve_run_mode(explicit="balanced", env=ci)
    quality = resolve_run_mode(explicit="quality-lean", env=ci)

    # The LEVER VALUES themselves differ across the three modes (the inputs the
    # entrypoint fans out) — un-fakeable by a constant.
    assert cost.max_workers < (quality.max_workers or 99)
    assert (
        cost.budget_ceiling_factor < balanced.budget_ceiling_factor < quality.budget_ceiling_factor
    )
    assert cost.budget_headroom < balanced.budget_headroom < quality.budget_headroom

    # Observe the fleet lever end to end: 5 independent qa tasks, a budget tuned so
    # cost-lean's smaller pool + max_workers=2 cap serializes harder than quality-
    # lean's bigger pool + wider cap. Use a budget big enough that the POOL is not
    # the binding constraint for quality-lean but the max_workers cap differs.
    tasks = _disjoint_tasks(5)
    cost_runner = _run_host_mode(
        tasks, _ConcurrencyRunner(), tmp_path, run_mode=cost, budget_tokens=10_000_000
    )
    quality_runner = _run_host_mode(
        tasks, _ConcurrencyRunner(), tmp_path, run_mode=quality, budget_tokens=10_000_000
    )
    # cost-lean caps fan-out at 2; quality-lean allows up to 5 → strictly wider.
    assert cost_runner.max_concurrency <= 2
    assert quality_runner.max_concurrency > cost_runner.max_concurrency

    # The posture lever also flows: a security role under quality-lean stays opus;
    # under cost-lean it STILL stays opus (hard floor) — but a plain qa task differs.
    qa_cost = _run_host_mode(
        [_two_task_dag()[0] | {"phase": "qa", "task_id": "QA"}],
        _ConcurrencyRunner(),
        tmp_path,
        run_mode=cost,
        budget_tokens=10_000_000,
    )
    qa_quality = _run_host_mode(
        [_two_task_dag()[0] | {"phase": "qa", "task_id": "QA"}],
        _ConcurrencyRunner(),
        tmp_path,
        run_mode=quality,
        budget_tokens=10_000_000,
    )
    # qa base = sonnet: cost-lean → haiku, quality-lean → opus (distinct tiers).
    assert _val(_argv_for_task(qa_cost, "QA"), "--model") == "haiku"
    assert _val(_argv_for_task(qa_quality, "QA"), "--model") == "opus"


def test_rmode_neutral_mode_is_noop(tmp_path):
    """An EXPLICITLY-NEUTRAL run mode (balanced) produces byte-identical dispatch
    (--model + --max-budget-usd argv) to the pre-M6b-2 path. Confirms the neutral
    posture == today's recommend output — the byte-identity mechanism the no-op
    rests on.

    NOTE (FIX 3): this proves the NEUTRAL-mode no-op only. It deliberately does NOT
    test run_mode=None — the real default-None path resolves to cost-lean (the saved
    profile), which is NON-neutral; that observed behavior is asserted separately by
    ``test_rmode_default_none_resolves_cost_lean_not_noop``."""
    from scripts.run_mode import resolve_run_mode

    balanced = resolve_run_mode(explicit="balanced", env={"CI": "1"})
    assert balanced.is_neutral is True  # the mechanism under test is the neutral one

    tasks = _two_task_dag()
    base_runner = FakeCliRunner(structured_output=_echo_envelope)
    _run_host_mode(tasks, base_runner, tmp_path, run_mode=balanced, budget_tokens=10_000_000)

    # Reference: the bridge-identical recommend output (posture=None == neutral).
    from scripts.model_tier import recommend

    for tid, expect in (("DOC", "haiku"), ("SEC", "opus")):
        argv = _argv_for_task(base_runner, tid)
        # neutral balanced posture == today's recommend (no posture).
        persona = next(t for t in tasks if t["task_id"] == tid)["assigned_persona"]
        assert _val(argv, "--model") == recommend(phase="doc", role_id=persona, posture="neutral")
        assert _val(argv, "--model") == recommend(phase="doc", role_id=persona)  # == no posture
        assert _val(argv, "--model") == expect
        # --max-budget-usd is est-derived (pool-independent) → tracks the tier only.
        assert (
            _val(argv, "--max-budget-usd")
            == f"{max_budget_usd_for(_cli_default_est_for(expect)):.2f}"
        )


def test_rmode_default_none_resolves_cost_lean_not_noop(tmp_path):
    """FIX 3 — the REAL default-None path: run_host_pipeline_for_project called with
    NO run_mode (the unthreaded default) auto-resolves to the saved-profile default
    (cost-effective → cost-lean), which is NON-neutral. This documents that
    run_mode=None is NOT a no-op — it narrows the BudgetPool ceiling AND caps
    concurrency at 2 (cost-lean's max_workers), AND down-biases tiers.

    Un-fakeable: 5 independent qa tasks on a budget large enough that a NEUTRAL run
    would fan out wider than 2, but the cost-lean default caps it at <=2 — so the
    observed concurrency is strictly the cost-lean lever, not a pool coincidence."""
    from scripts.model_tier import recommend
    from scripts.run_mode import default_mode_id, resolve_run_mode

    # Sanity: the saved-profile default IS cost-lean (the fixed maintainer decision).
    assert default_mode_id() == "cost-lean"
    assert resolve_run_mode(env={"CI": "1"}).mode_id == "cost-lean"  # no higher rung

    tasks = _disjoint_tasks(5)

    # (1) DEFAULT-NONE run: do NOT thread run_mode at all → auto-resolves cost-lean.
    from scripts.result_journal import ResultJournal

    clone = tmp_path / "experiment" / "none"
    clone.mkdir(parents=True, exist_ok=True)
    none_runner = _ConcurrencyRunner()
    asyncio.run(
        run_host_pipeline_for_project(
            tasks,
            clone_dir=str(clone),
            budget=BudgetPool(total_tokens=10_000_000),
            journal=ResultJournal(),
            runner=none_runner,
            env={"CI": "1"},  # non-interactive; no ATELIER_RUN_MODE pin
            # run_mode intentionally OMITTED — exercises the real default-None path.
            max_workers=5,
        )
    )
    # (2) NEUTRAL (balanced) run on the SAME tasks+budget → wider fan-out.
    balanced = resolve_run_mode(explicit="balanced", env={"CI": "1"})
    neutral_runner = _run_host_mode(
        tasks, _ConcurrencyRunner(), tmp_path, run_mode=balanced, budget_tokens=10_000_000
    )

    # THE COST-LEAN-DEFAULT ASSERTION: the unthreaded default narrows concurrency to
    # cost-lean's max_workers cap (2), strictly tighter than the neutral run. A
    # genuine no-op would have matched the neutral run's wider fan-out.
    assert none_runner.max_concurrency <= 2, (
        "default-None must auto-resolve cost-lean (max_workers=2) — NOT a neutral no-op"
    )
    assert neutral_runner.max_concurrency > none_runner.max_concurrency, (
        "the NEUTRAL run fans out wider than the cost-lean default — proving "
        "default-None is the narrowed cost-lean posture, not a no-op"
    )

    # The posture lever also bites on the default-None path: a qa task (base sonnet)
    # is down-biased to haiku under the cost-lean default.
    qa_default = _ConcurrencyRunner()
    clone2 = tmp_path / "experiment" / "none-qa"
    clone2.mkdir(parents=True, exist_ok=True)
    asyncio.run(
        run_host_pipeline_for_project(
            [_disjoint_tasks(1)[0]],
            clone_dir=str(clone2),
            budget=BudgetPool(total_tokens=10_000_000),
            journal=ResultJournal(),
            runner=qa_default,
            env={"CI": "1"},
            max_workers=5,
        )
    )
    # qa base = sonnet; cost-lean default biases DOWN to haiku (NOT the neutral sonnet).
    assert _val(_argv_for_task(qa_default, "t0"), "--model") == "haiku"
    assert recommend(phase="qa", posture="cost-lean") == "haiku" != recommend(phase="qa")


def test_rmode_never_writes_settings_json(tmp_path, monkeypatch):
    """Resolving + applying a RunMode for a run does NOT call apply_profile / write
    ~/.claude/settings.json. The orchestrator model is ADVISORY only. Spy the
    writer + the apply path; assert ZERO writes.

    RED pre-fix: scripts.run_mode does not exist (ImportError)."""
    import scripts.recommended_settings as rs_mod
    from scripts.run_mode import resolve_run_mode

    apply_calls = []
    write_calls = []
    monkeypatch.setattr(rs_mod, "apply_profile", lambda *a, **k: apply_calls.append((a, k)))
    monkeypatch.setattr(rs_mod, "apply_recommended", lambda *a, **k: apply_calls.append((a, k)))
    monkeypatch.setattr(rs_mod, "_atomic_write_json", lambda *a, **k: write_calls.append((a, k)))

    # Resolve a quality-lean mode (which has an advisory opus orchestrator model)
    # and drive a real run through the host entrypoint with it.
    rmode = resolve_run_mode(explicit="quality-lean", env={"CI": "1"})
    assert rmode.orchestrator_model == "opus"  # ADVISORY value present...
    runner = FakeCliRunner(structured_output=_echo_envelope)
    _run_host_mode(_two_task_dag(), runner, tmp_path, run_mode=rmode, budget_tokens=10_000_000)

    # ...but NOTHING wrote settings.json — R-MODE is transient/per-run.
    assert apply_calls == [], "R-MODE must NOT call apply_profile (advisory only)"
    assert write_calls == [], "R-MODE must NOT write settings.json"
