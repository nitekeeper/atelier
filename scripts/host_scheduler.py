"""host_scheduler — the deterministic host's two scheduling façades (M4).

This module exposes two coroutine façades over the KEPT engine + the M3 CLI
adapter, plus the file-race-freedom mechanism (per-writer git worktrees):

* :func:`parallel` — the EXPLICIT BARRIER.  All tasks in the group run
  concurrently and the group is a hard barrier: the next group cannot start
  until every task here is TERMINAL-ONLY.  This is *exactly*
  ``pm_dispatch.partition_waves`` + the wave-gate (``WaveTracker.terminal_only``
  / ``wave_gate_satisfied``) re-exposed.  It REUSES the production
  :class:`~scripts.pm_dispatch.WaveDispatcher` driving M3's
  :class:`~scripts.cli_dispatch.CliDispatchTools` through the same
  ``spawn_fn`` / ``poll_fn`` / ``sleep_fn`` seams the bridge used — it does NOT
  reimplement the barrier.

* :func:`pipeline` — BARRIER-FREE advance ALONG PROVEN-INDEPENDENT DAG edges.
  Each task advances the instant its OWN chain is satisfied:
  ``ready(t) = all(dep TERMINAL for dep in depends_on(t)) and
  write_disjoint(t, in_flight)``.  Wall-clock is the slowest single-item chain,
  NOT the sum of per-stage maxima.

  **FAIL-CLOSED.** A task is admitted barrier-free ONLY when
  ``dag_proof.independent(t, u)`` is True for EVERY in-flight ``u``.  Absence of
  an independence proof ⇒ the task waits (it falls back to barrier semantics —
  it is held until the conflicting in-flight task drains).  A write-conflict, a
  dependency edge, or simply a missing proof all hold the task back: there is no
  silent overlap.  ``write_disjoint`` is additionally re-checked DYNAMICALLY
  against the live in-flight set as a defense-in-depth backstop, so even a
  ``DagProof`` that wrongly claimed two write-overlapping tasks independent
  could never run them concurrently.

  Concurrency is bounded by a shared ``asyncio.Semaphore(MAX_PARALLEL_WORKERS)``
  (idea 3 — the same counter the fix loop will later share) NARROWED to the
  remaining budget via ``BudgetPool.static_fleet_width`` (idea 2 — fan-out only
  ever narrows past ``MAX_PARALLEL_WORKERS``, never widens).

File-race-freedom under barrier-free advance
--------------------------------------------
Even with the DAG gate proving within-group write-disjointness, two concurrent
writer tasks to DIFFERENT paths in the SAME clone still share one working tree —
a physical race on the index / refs.  So :func:`pipeline` (when a
``worktree_factory`` is supplied) gives each concurrently-dispatched WRITER its
OWN git worktree (writes physically isolated), then MERGES completed worktrees
back into the base clone DETERMINISTICALLY in dependency order
(``(parallel_group, task_id)``).  Because writes are disjoint by construction
(gate 3 + the dynamic re-check), the merges are conflict-free — asserted, never
hoped.  If worktree isolation is infeasible for a case, the scheduler falls back
to the barrier (fail-closed): the writer is held until it can run alone.

Determinism
-----------
No scheduling decision reads a wall clock or RNG.  Readiness is a pure function
of TERMINAL state + the (immutable) ``DagProof``.  Tie-breaking among ready
tasks is the deterministic ``(parallel_group, task_id)`` order.  An injectable
``clock`` is threaded into the engine seams for ``parallel`` (the WaveDispatcher
owns the wall-clock deadline) but is NEVER consulted to make a ``pipeline``
admission decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.budget_pool import BudgetPool
from scripts.cli_dispatch import (
    DEFAULT_DISALLOWED_TOOLS as _DEFAULT_DISALLOWED_TOOLS,
)
from scripts.cli_dispatch import (
    DEFAULT_PERMISSION_MODE as _DEFAULT_PERMISSION_MODE,
)
from scripts.cli_dispatch import (
    FAILED_ATTEMPT,
    CliDispatchTools,
    Runner,
    SandboxWrap,
    build_cli_poll_fn,
    build_cli_spawn_fn,
    direct_upstream_hashes,
    identity_sandbox_wrap,
    is_failed_attempt,
    real_cli_runner,
    run_attempt,
)
from scripts.dag import DagProof
from scripts.git_utils import git as _git
from scripts.pm_dispatch import (
    MAX_PARALLEL_WORKERS,
    WALL_CLOCK_S,
    WaveDispatcher,
)
from scripts.result_journal import ResultJournal

__all__ = [
    "MAX_PARALLEL_WORKERS",
    "JournalKeyTracker",
    "WorktreeError",
    "parallel",
    "pipeline",
    "scheduler_upstream_hashes_for",
    "simple_worktree_factory",
]


# ── (1) DagProof.reads_from → journal upstream hashes wiring ─────────────────


class JournalKeyTracker:
    """Maps ``task_id → journal_key`` so direct reads-from upstream ENVELOPE
    hashes resolve correctly (the wiring M3 deferred to M4).

    M3's :func:`scripts.cli_dispatch.direct_upstream_hashes` resolves an
    upstream's envelope hash via ``journal.get_envelope_hash(<key>)`` — but the
    journal is keyed by the full CONTENT key, not the raw ``task_id``.  This
    tracker records, after each successful dispatch, the journal KEY that a task
    was journaled under, and exposes a tiny ``get_envelope_hash(task_id)`` shim
    that ``direct_upstream_hashes`` can call by raw ``task_id``.

    A task with no recorded key (not yet TERMINAL) contributes NO hash — exactly
    the M1 contract: an incomplete upstream is not yet replayable, so the
    downstream key naturally misses until the upstream lands.
    """

    def __init__(self, journal: ResultJournal) -> None:
        self._journal = journal
        self._key_for: dict[str, str] = {}

    def record(self, task_id: str, journal_key: str) -> None:
        """Record the journal key a *task_id* was journaled under."""
        self._key_for[str(task_id)] = journal_key

    def get_envelope_hash(self, task_id: str) -> str | None:
        """Resolve *task_id*'s envelope hash via its recorded journal key.

        Signature-compatible with ``ResultJournal.get_envelope_hash`` so it can
        be passed to :func:`scripts.cli_dispatch.direct_upstream_hashes` as the
        ``journal`` argument's hash resolver.
        """
        key = self._key_for.get(str(task_id))
        if key is None:
            return None
        return self._journal.get_envelope_hash(key)


def scheduler_upstream_hashes_for(
    task: Mapping[str, Any],
    dag_proof: DagProof,
    key_tracker: JournalKeyTracker,
) -> frozenset[str]:
    """Compute a task's DIRECT reads-from upstream envelope hashes.

    Sourced from ``dag_proof.reads_from(task_id)`` → each upstream's journaled
    ``envelope_hash`` (resolved by ``task_id`` through *key_tracker*).  A changed
    upstream (different envelope → different hash) yields a different downstream
    key → a forced re-dispatch (idea 4 content-chaining).  This is the value the
    scheduler passes as ``upstream_envelope_hashes`` to ``run_attempt``.
    """
    task_id = str(task.get("task_id", ""))
    # ``direct_upstream_hashes`` calls ``journal.get_envelope_hash(up_id)`` — we
    # pass the key_tracker (which resolves by task_id) in the journal slot.
    return direct_upstream_hashes(task_id, dag_proof, key_tracker)


# ── helpers shared by both façades ──────────────────────────────────────────


def _task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("task_id", ""))


def _depends_on(task: Mapping[str, Any]) -> list[str]:
    return [str(d) for d in (task.get("depends_on") or [])]


def _writes(task: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(str(p) for p in (task.get("writes") or []))


def _is_writer(task: Mapping[str, Any]) -> bool:
    return bool(_writes(task))


def _sort_key(task: Mapping[str, Any]) -> tuple[int, str]:
    """Deterministic ordering: (parallel_group, task_id).  Used for ready-task
    tie-breaking AND for the deterministic worktree merge order — both must be
    clock/RNG-free."""
    return (int(task.get("parallel_group", 0)), _task_id(task))


def _default_est_for(model: str) -> int:
    """Cold-start per-agent output-token estimate by tier (mirrors
    ``cli_dispatch._default_est_for`` — kept local so the fleet-width seed does
    not import a private name)."""
    return {"haiku": 2_000, "sonnet": 6_000, "opus": 12_000}.get(model, 6_000)


# ── (3) Worktree isolation ──────────────────────────────────────────────────

#: Fixed INTERNAL git identity for the engine's own commits (writer-result
#: commits + the merge-back commit).  The engine must be SELF-CONTAINED — it must
#: NOT depend on an ambient/global ``user.name``/``user.email`` (a clean CI
#: runner has none, and a fresh target-repo clone won't reliably have one
#: either).  Supplied per-invocation via ``git -c user.*`` (see
#: :func:`_identity_commit_args`) so it never mutates the repo's config or the
#: process env, and is consistent across every commit the engine makes.
_ENGINE_GIT_NAME = "atelier"
_ENGINE_GIT_EMAIL = "atelier@localhost"


def _identity_commit_args(args: list[str]) -> list[str]:
    """Prepend the fixed engine identity to a git ``args`` list that creates a
    commit (``commit`` / ``merge``).

    Returns ``["-c", "user.name=…", "-c", "user.email=…", *args]`` so the
    identity is scoped to THIS one invocation — no ``git config`` write, no env
    mutation.  Use for EVERY commit-creating call so the engine never relies on
    ambient git config.
    """
    return [
        "-c",
        f"user.name={_ENGINE_GIT_NAME}",
        "-c",
        f"user.email={_ENGINE_GIT_EMAIL}",
        *args,
    ]


class WorktreeError(RuntimeError):
    """A worktree create/merge operation failed.  The scheduler treats this as
    a signal to fall back to the barrier (fail-closed) rather than risk a shared
    working tree."""


class _Worktree:
    """One per-writer git worktree (its own working directory + branch) carved
    off the base clone, so a concurrent writer's edits are physically isolated.

    ``merge_back`` fast-forwards / merges the worktree branch into the base
    clone's current branch.  Because the scheduler only isolates WRITE-DISJOINT
    tasks (gate 3 + the dynamic re-check), distinct worktrees touch distinct
    paths and the merge is conflict-free by construction — asserted in
    :func:`pipeline`'s merge step.
    """

    def __init__(self, *, path: Path, branch: str, base_clone: Path) -> None:
        self.path = path
        self.branch = branch
        self.base_clone = base_clone


#: How many times to retry a ``git worktree add`` that fails on the transient
#: ``.git/worktrees/`` admin race (exit 128) before giving up.  The race is
#: caused by concurrent worktree-admin git ops touching the shared admin
#: directory; the scheduler serializes those ops with ``worktree_admin_lock``, so
#: in practice the retry almost never fires — it is belt-and-suspenders against a
#: residual filesystem-level race (e.g. NFS / WSL2 metadata lag).  *(MAJOR-1)*
_WORKTREE_ADD_RETRIES = 4
#: Tiny backoff between worktree-add retries (seconds).  Deterministic-enough: it
#: only affects WALL-CLOCK on the rare retry path, never a scheduling decision.
_WORKTREE_ADD_RETRY_BACKOFF_S = 0.05


def simple_worktree_factory(base_clone: str | Path) -> Callable[[str], _Worktree]:
    """Return a ``factory(task_id) -> _Worktree`` that carves a fresh git
    worktree + branch off *base_clone* for one writer task.

    Each worktree lives at ``<base_clone>/.atelier-worktrees/<task_id>`` on a
    branch ``atelier/wt/<task_id>``.  Deterministic naming (task-id derived, no
    clock/RNG) so a replay reproduces the same layout.  Raises
    :class:`WorktreeError` on a persistent git failure.

    **MAJOR-1 concurrency safety.** ``git worktree add`` mutates the shared
    ``.git/worktrees/`` admin directory; concurrent adds (and adds racing a
    concurrent ``git merge`` removing a sibling worktree) intermittently fail with
    ``fatal: failed to read .git/worktrees/… (exit 128)``.  Two defenses combine:

    1. The scheduler serializes EVERY worktree-admin git op (add / merge / remove
       / prune) under a single ``worktree_admin_lock`` — only the FAST git-admin
       step is serialized; the slow agent run inside the worktree stays
       concurrent.  See :func:`pipeline`.
    2. This factory ALSO retries the transient exit-128 admin race a few times
       (:data:`_WORKTREE_ADD_RETRIES`) as a residual backstop, cleaning any
       partial worktree dir between attempts so the retry is a fresh add.

    With (1)+(2) a worktree-add failure is essentially impossible; if one DOES
    persist, this raises :class:`WorktreeError` and the scheduler FAILS THE
    ATTEMPT (it never runs the writer un-isolated in the shared base — see
    :func:`pipeline`).
    """
    base = Path(base_clone).resolve()

    def factory(task_id: str) -> _Worktree:
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(task_id))
        wt_root = base / ".atelier-worktrees"
        wt_path = wt_root / safe
        branch = f"atelier/wt/{safe}"
        wt_root.mkdir(parents=True, exist_ok=True)
        last_err = ""
        for attempt_i in range(_WORKTREE_ADD_RETRIES):
            # Add a detached-then-branched worktree off the current HEAD.
            res = _git(
                ["worktree", "add", "-b", branch, str(wt_path), "HEAD"],
                base,
                check=False,
            )
            if res.returncode == 0:
                return _Worktree(path=wt_path, branch=branch, base_clone=base)
            last_err = res.stderr.strip()[:300]
            # Transient admin race → clean any half-created residue and retry so
            # the next add starts fresh (a stale `wt_path` dir or a dangling admin
            # entry would otherwise make the retry fail with "already exists").
            _git(["worktree", "remove", str(wt_path), "--force"], base, check=False)
            _git(["worktree", "prune"], base, check=False)
            _git(["branch", "-D", branch], base, check=False)
            if attempt_i < _WORKTREE_ADD_RETRIES - 1:
                time.sleep(_WORKTREE_ADD_RETRY_BACKOFF_S)
        raise WorktreeError(
            f"git worktree add failed for {task_id!r} after "
            f"{_WORKTREE_ADD_RETRIES} attempts: {last_err}"
        )

    return factory


def _merge_worktree(wt: _Worktree) -> None:
    """Commit a writer worktree's changes and merge its branch into the base
    clone, then remove the worktree.  Conflict-free by construction (disjoint
    writes); a conflict here is a SCHEDULER bug (the disjointness invariant was
    violated) and is surfaced loudly, never silently resolved.
    """
    base = wt.base_clone
    # Commit any pending writer changes on the worktree branch.  The commit
    # carries the engine's OWN fixed identity (`-c user.*`) so it never depends
    # on ambient/global git config (a clean CI runner / fresh clone has none).
    status = _git(["status", "--porcelain"], wt.path, check=False)
    if status.stdout.strip():
        _git(["add", "-A"], wt.path, check=False)
        commit = _git(
            _identity_commit_args(["commit", "-m", f"atelier worktree result [{wt.branch}]"]),
            wt.path,
            check=False,
        )
        if commit.returncode != 0:
            raise WorktreeError(
                f"committing worktree {wt.branch!r} failed: {commit.stderr.strip()[:300]}"
            )

    # Merge into the base clone's current branch.  --no-ff keeps an explicit
    # merge record; disjoint writes mean no conflict.  --no-ff CREATES a merge
    # commit, so this also needs the engine identity (same fixed `-c user.*`).
    merge = _git(
        _identity_commit_args(["merge", "--no-ff", wt.branch, "-m", f"Merge {wt.branch}"]),
        base,
        check=False,
    )
    if merge.returncode != 0:
        _git(["merge", "--abort"], base, check=False)
        raise WorktreeError(
            f"INVARIANT VIOLATION: merging worktree {wt.branch!r} CONFLICTED — "
            "two concurrently-isolated writers must be write-disjoint by "
            "construction (gate 3 + dynamic re-check), so a conflict means the "
            f"disjointness invariant was breached. git: {merge.stderr.strip()[:300]}"
        )

    # Remove the worktree + delete its branch (best-effort cleanup).
    _remove_worktree(wt)


def _list_linked_worktrees(base: Path) -> list[Path]:
    """Return the paths of every LINKED worktree of the base clone (excluding the
    main working tree itself).

    Parses ``git worktree list --porcelain`` (the stable, scriptable form: one
    ``worktree <path>`` line per tree, the first being the main repo).  Used by
    :func:`_cleanup_after_merge_failure` so that under eager concurrent merge-back
    — where the caller's named-leftover snapshot can miss a just-finished worktree
    — cleanup removes EVERY linked worktree, not only the ones it was handed.
    Best-effort + non-raising (``check=False``)."""
    res = _git(["worktree", "list", "--porcelain"], base, check=False)
    if res.returncode != 0:
        return []
    paths: list[Path] = []
    main_seen = False
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line[len("worktree ") :].strip())
            if not main_seen:
                main_seen = True  # the first entry is the main working tree
                continue
            paths.append(p)
    return paths


def _remove_worktree(wt: _Worktree) -> None:
    """Best-effort: remove *wt*'s worktree directory + delete its branch.

    Idempotent and non-raising (``check=False``): a worktree already gone, or a
    branch already deleted, is fine.  Used both after a successful merge and to
    DISCARD an isolated worktree that must not be merged (a failed writer's
    partial output, or remaining worktrees during error cleanup).
    """
    base = wt.base_clone
    _git(["worktree", "remove", str(wt.path), "--force"], base, check=False)
    _git(["worktree", "prune"], base, check=False)
    _git(["branch", "-D", wt.branch], base, check=False)


def _discard_worktree(wt: _Worktree) -> None:
    """Discard an isolated worktree WITHOUT merging it (MINOR-1).

    A writer that failed mid-write leaves dirty/partial files in its worktree;
    merging them would land partial garbage in the base.  We therefore drop the
    worktree entirely — its writes never reach the base clone.  Non-raising.
    """
    _remove_worktree(wt)


def _cleanup_after_merge_failure(
    clone_dir: str | os.PathLike[str],
    leftover: Sequence[_Worktree],
) -> None:
    """Restore a CLEAN base clone after a mid-loop merge failure (MAJOR-2).

    On ANY merge error the merge loop calls this so a caller's fallback never
    inherits a polluted clone:

    1. Abort any half-applied merge in the base (idempotent — the conflict path
       in :func:`_merge_worktree` already aborted, but a non-conflict git failure
       may have left a partial merge).
    2. Remove EVERY remaining (un-merged) worktree + its branch.
    3. Restore the base working tree to ``HEAD`` (``reset --hard``) and drop the
       ``.atelier-worktrees`` residue so ``git status --porcelain`` is empty.

    Best-effort + non-raising: cleanup must not mask the original error the
    caller is about to see re-raised.
    """
    base = Path(clone_dir).resolve()
    # 1. Abort any in-progress merge left behind by a non-conflict git failure.
    _git(["merge", "--abort"], base, check=False)
    # 2. Remove every un-merged worktree (and prune stale admin entries).  Under
    #    eager (per-completion, concurrent) merge-back the caller's `leftover`
    #    snapshot may miss a worktree that finished but had not yet merged, so we
    #    also remove EVERY remaining linked worktree git knows about — not just
    #    the named leftovers — to be exhaustive.
    for wt in leftover:
        _remove_worktree(wt)
    for wt_path in _list_linked_worktrees(base):
        _git(["worktree", "remove", str(wt_path), "--force"], base, check=False)
    # 3. Restore the base working tree to a clean HEAD.  reset --hard drops any
    #    partially-merged tracked changes; clean removes the worktrees dir +
    #    any other untracked residue so `git status --porcelain` is empty.
    #
    #    MINOR-1 (autocrlf): on a host with `core.autocrlf=true`, a plain
    #    `reset --hard` re-applies CRLF normalization and can leave a FALSE-dirty
    #    `M <file>` (an LF↔CRLF-only diff, not a real change), so the cleanup would
    #    report a dirty base even though nothing changed.  We pin
    #    `core.autocrlf=false` + `core.eol=lf` for THIS invocation (no config write)
    #    so the reset reproduces HEAD byte-for-byte regardless of the host's
    #    autocrlf setting — the cleanup is clean on any host.
    _git(
        ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "reset", "--hard", "HEAD"],
        base,
        check=False,
    )
    _git(["clean", "-ffdx", ".atelier-worktrees"], base, check=False)
    # 4. A final prune reaps any worktree whose DIRECTORY was removed by `clean`
    #    above but whose admin entry git still lists as "prunable" — otherwise
    #    `git worktree list` would report a stale (prunable) leak.
    _git(["worktree", "prune"], base, check=False)


# ── (2) parallel() — the EXPLICIT BARRIER (reuse WaveDispatcher) ─────────────


async def parallel(
    tasks: Sequence[Mapping[str, Any]],
    *,
    dispatcher: CliDispatchTools,
    db_path: str,
    budget: BudgetPool,
    dag_proof: DagProof,
    clock: Callable[[], float] | None = None,
) -> list[dict[str, Any]]:
    """Run *tasks* as the EXPLICIT BARRIER group (atelier's wave semantics).

    All tasks run concurrently AND the group is a hard barrier: this returns
    only once every task is TERMINAL-ONLY (``wave_gate_satisfied`` /
    ``WaveTracker.terminal_only``).  This REUSES the production
    :class:`~scripts.pm_dispatch.WaveDispatcher` driving *dispatcher* (M3's
    :class:`CliDispatchTools`) through the ``spawn_fn`` / ``poll_fn`` /
    ``sleep_fn`` seams — the barrier is NOT reimplemented here.

    ``db_path`` is the engine's task-state store (the WaveDispatcher records
    attempt counts + terminal status there; each *tasks* dict's ``id`` must be a
    row id in it, and the dict's ``task_id`` — which *dispatcher* keys on — must
    equal that ``id``).

    ``budget`` / ``dag_proof`` — **:no-op: in this façade.**  They are accepted
    ONLY for call-site symmetry with :func:`pipeline` (so an orchestrator can
    dispatch either façade with one uniform keyword set).  The barrier path does
    NOT consult them: the budget is already owned + enforced inside *dispatcher*
    (via ``run_attempt``'s ``assert_can_dispatch``), and the explicit barrier
    needs no independence proof (every task waits for the whole wave regardless).
    They are explicitly ``del``-ed below so a future edit cannot silently grow a
    dependency on an argument this façade does not honor.

    Returns the per-wave summary list the WaveDispatcher produces.

    The WaveDispatcher is synchronous (it owns the wall-clock deadline + the
    single re-queue site).  We run it in a thread so this façade is awaitable and
    composes with an ``asyncio`` caller, WITHOUT duplicating the engine loop.
    ``dispatcher.pump`` (wired to the engine ``sleep_fn``) drains the in-flight
    ``run_attempt`` futures between polls on *dispatcher*'s owned loop.
    """
    # MINOR-2: make the inertness explicit — these are surface-symmetry args the
    # barrier path does not honor (budget enforced in `dispatcher`; barrier needs
    # no proof).  `del` makes any accidental future use a hard NameError.
    del budget, dag_proof

    spawn_fn = build_cli_spawn_fn(dispatcher)
    poll_fn = build_cli_poll_fn(dispatcher)

    def sleep_fn(_seconds: float) -> None:
        # Each poll round that made no progress drains the scheduled
        # run_attempt coroutines on the dispatcher's owned loop.
        dispatcher.pump()

    engine_kwargs: dict[str, Any] = {
        "spawn_fn": spawn_fn,
        "poll_fn": poll_fn,
        "sleep_fn": sleep_fn,
    }
    if clock is not None:
        engine_kwargs["clock"] = clock

    engine = WaveDispatcher(db_path, **engine_kwargs)

    # The engine is synchronous; run it off the event loop so this stays
    # awaitable.  It drives `dispatcher`'s OWN loop via `pump()` — there is no
    # nested running loop, so this is safe.
    return await asyncio.to_thread(engine.run, list(tasks))


# ── (2)+(3) pipeline() — BARRIER-FREE along proven-independent edges ─────────


async def pipeline(
    tasks: Sequence[Mapping[str, Any]],
    *,
    budget: BudgetPool,
    journal: ResultJournal,
    dag_proof: DagProof,
    model_for: Callable[[Mapping[str, Any], int], str],
    briefing_for: Callable[[Mapping[str, Any], int], str],
    clone_dir: str | Path,
    worktree_factory: Callable[[str], _Worktree] | None = None,
    runner: Runner = real_cli_runner,
    est_for: Callable[[str], int] = _default_est_for,
    max_workers: int = MAX_PARALLEL_WORKERS,
    wall_clock_s: float = WALL_CLOCK_S,
    permission_mode: str = _DEFAULT_PERMISSION_MODE,
    disallowed_tools: Sequence[str] = _DEFAULT_DISALLOWED_TOOLS,
    allowed_tools: Sequence[str] | None = None,
    sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
) -> list[dict[str, Any]]:
    """Advance *tasks* BARRIER-FREE along proven-independent DAG edges.

    A task is admitted the instant its OWN chain is satisfied — it does NOT wait
    on a whole-wave barrier.  Readiness:

        ready(t) = all(dep TERMINAL for dep in depends_on(t))
                   AND write_disjoint(t, in_flight)
                   AND dag_proof.independent(t, u) for every in-flight u

    **FAIL-CLOSED:** the LAST clause means a task is admitted concurrently ONLY
    when the ``DagProof`` PROVES it independent of every in-flight task.  No
    proof ⇒ the task waits until the conflicting task drains (barrier fallback).
    ``write_disjoint`` is re-checked dynamically as a defense-in-depth backstop,
    so two write-overlapping tasks are NEVER concurrently in-flight even if the
    proof were wrong.

    Concurrency is bounded by a shared ``asyncio.Semaphore`` whose width is
    ``static_fleet_width(budget, per_agent, max_workers)`` — fan-out narrows to
    the remaining budget and never exceeds *max_workers*.

    When *worktree_factory* is supplied, each concurrently-dispatched WRITER
    runs in its OWN git worktree (physical write isolation); completed worktrees
    are merged back into *clone_dir* deterministically in dependency order.

    Returns the validated envelope (or failed-attempt sentinel) per task, in the
    deterministic ``(parallel_group, task_id)`` order.
    """
    ordered = sorted(tasks, key=_sort_key)
    key_tracker = JournalKeyTracker(journal)

    # Shared concurrency counter (idea 3).  Sized to the remaining budget — only
    # ever NARROWS max_workers (idea 2).  At least 1 so a single ready task can
    # always make progress (the budget gate inside run_attempt is the real stop).
    fleet = BudgetPool.static_fleet_width(
        budget,
        per_agent_tokens=_seed_per_agent(ordered, model_for, est_for),
        max_workers=max_workers,
    )
    sem_width = max(1, fleet)
    semaphore = asyncio.Semaphore(sem_width)

    terminal: set[str] = set()  # task_ids whose attempt resolved (done/failed)
    results: dict[str, Any] = {}
    in_flight: dict[str, Mapping[str, Any]] = {}
    # Worktree per in-flight writer (None when not isolated).  Each successful
    # writer's worktree is merged back into the base clone EAGERLY (the instant
    # it is terminal) and popped from this map — see the eager-merge step in
    # `_run_one`.  Whatever remains here on an error path is leftover to clean up.
    writer_worktrees: dict[str, _Worktree] = {}

    lock = asyncio.Lock()
    progress = asyncio.Condition(lock)
    # Serializes EVERY worktree-admin git op that mutates the shared
    # `.git/worktrees/` admin dir or the base index/refs: worktree CREATE (`git
    # worktree add`), MERGE-back (which also removes the worktree), DISCARD, and
    # the error-path cleanup sweep.  *(MAJOR-1)* `git worktree add` races against
    # a concurrent add/merge on `.git/worktrees/` and intermittently fails
    # (exit 128) — which previously dropped the writer to an UN-ISOLATED run in
    # the shared base, defeating file-race-freedom under fan-out.  Serializing
    # only the FAST git-admin step (the slow agent run inside the worktree stays
    # concurrent) closes that race.  Distinct from `progress` so a (blocking,
    # thread-offloaded) git op never holds the admission lock.  Deadlock-free: a
    # task acquires it for `add` at the START of its run and (separately, never
    # nested) for `merge`/`discard` at the END — the two are sequential within a
    # task and never held across `progress.wait()`.
    worktree_admin_lock = asyncio.Lock()

    def _ready(task: Mapping[str, Any], live: dict[str, Mapping[str, Any]]) -> bool:
        """Pure readiness predicate (no clock, no RNG).  See module docstring.

        ``live`` is the current in-flight set (excludes *task* itself).
        """
        tid = _task_id(task)
        # 1. Every upstream dependency must be TERMINAL.
        for dep in _depends_on(task):
            if dep not in terminal:
                return False
        # 2+3. Independence vs every in-flight task — FAIL-CLOSED: a missing
        #      proof, a write-conflict, or a dep edge all block admission.
        t_writes = _writes(task)
        for other_id, other in live.items():
            if other_id == tid:
                continue
            # Dynamic write-disjointness backstop (defense-in-depth): even if the
            # proof wrongly claimed independence, overlapping writes block.
            if t_writes & _writes(other):
                return False
            # The proof gate: absence of a proof ⇒ NOT independent ⇒ block.
            if not dag_proof.independent(tid, other_id):
                return False
        return True

    async def _run_one(task: Mapping[str, Any]) -> None:
        # ``tid`` is a plain dict read the admission loop already performed to
        # spawn this task, so it cannot raise here; the ``finally`` keys on it.
        tid = _task_id(task)
        result: Any = None
        success = False
        jkey: str | None = None
        try:
            # MAJOR-1: the production seams (`_attempt_for` / `model_for` /
            # `briefing_for`) are FALLIBLE (unknown persona, missing template) —
            # they MUST run inside the ``try`` so a raise routes through the
            # ``finally`` (mark terminal + notify) instead of hanging the
            # admission loop on a completion that never arrives.
            attempt = _attempt_for(task)
            model = model_for(task, attempt)
            briefing = briefing_for(task, attempt)
            async with semaphore:
                # Decide on isolation for a writer (when a factory is wired).
                wt: _Worktree | None = None
                run_cwd: str | Path = clone_dir
                run_add_dir: str | Path = clone_dir
                if worktree_factory is not None and _is_writer(task):
                    # MAJOR-1: carve the worktree under the worktree-admin lock so
                    # the `.git/worktrees/` admin mutation is serialized against
                    # every other writer's add/merge/remove (the slow agent run
                    # below stays concurrent — only this fast git step is locked).
                    try:
                        async with worktree_admin_lock:
                            wt = await asyncio.to_thread(worktree_factory, tid)
                    except WorktreeError:
                        # SAFE-BY-CONSTRUCTION (MAJOR-1): isolation is mandatory for
                        # a writer under fan-out.  The old code fell back to running
                        # UN-ISOLATED in the shared base — but that put TWO writers
                        # in the same working tree concurrently (a race on
                        # `.git/index.lock`, outputs left uncommitted/untracked),
                        # defeating file-race-freedom.  Instead we FAIL THIS ATTEMPT:
                        # return the failed-attempt sentinel (no worktree ⇒ no merge
                        # ⇒ nothing lands in the base) so the engine re-queues /
                        # abandons it, and the pipeline NEVER runs two writers in the
                        # base tree.  `success` stays False ⇒ the `finally` marks it
                        # terminal as a failed attempt and notifies; downstreams of a
                        # failed writer simply never see it succeed.
                        result = FAILED_ATTEMPT
                        return
                    run_cwd = wt.path
                    run_add_dir = wt.path
                    async with lock:
                        writer_worktrees[tid] = wt

                up_hashes = list(scheduler_upstream_hashes_for(task, dag_proof, key_tracker))
                # The journal key this task is stored under (for the task_id→key
                # map so downstreams resolve our envelope hash).
                jkey = journal.key(
                    task,
                    attempt,
                    model=model,
                    briefing=briefing,
                    upstream_envelope_hashes=up_hashes,
                )
                result = await run_attempt(
                    task,
                    attempt,
                    budget=budget,
                    journal=journal,
                    model=model,
                    briefing=briefing,
                    clone_dir=clone_dir,
                    upstream_envelope_hashes=up_hashes,
                    runner=runner,
                    cwd=run_cwd,
                    add_dir=run_add_dir,
                    est_for=est_for,
                    wall_clock_s=wall_clock_s,
                    permission_mode=permission_mode,
                    disallowed_tools=disallowed_tools,
                    allowed_tools=allowed_tools,
                    sandbox_wrap=sandbox_wrap,
                )
                success = True
        finally:
            # ALWAYS mark terminal + notify, even on a raised exception
            # (BudgetExceeded / CloneEscapeError), so the admission loop never
            # hangs waiting on a completion that never arrives.  The exception
            # itself still propagates out of the coroutine (gathered after the
            # loop) — terminal state is bookkeeping, not suppression.
            succeeded = success and not is_failed_attempt(result)
            # Pull this task's worktree out of the in-flight map (under the lock)
            # WITHOUT yet marking the task terminal — the merge must land in base
            # HEAD *before* the terminal transition (see the ordering note below).
            async with progress:
                done_wt = writer_worktrees.pop(tid, None)

            # BUGFIX (dependent-writer reads-its-inputs): a successful writer's
            # worktree is merged into the base clone EAGERLY — the instant it
            # completes — instead of deferring all merges to after the admission
            # loop.  A downstream task carves its OWN worktree from base HEAD at
            # admission (`git worktree add … HEAD`), and that only fires once every
            # `depends_on` upstream is TERMINAL.  So the invariant a downstream
            # relies on is: TERMINAL ⟹ ALREADY MERGED INTO HEAD.  We therefore do
            # the (blocking, thread-offloaded) git merge/discard HERE — OUTSIDE the
            # `progress` lock (so the admission lock is never held across git) and
            # serialized by `worktree_admin_lock` (so two concurrent completions —
            # or a completion racing another writer's `git worktree add` — never
            # mutate the base index/refs or `.git/worktrees/` at once) — and ONLY
            # THEN, below, mark the task
            # terminal + notify.  The old code deferred the merge to after the loop,
            # leaving a downstream's worktree branched from a HEAD that did NOT yet
            # contain its upstreams' outputs, so the dependent agent could not READ
            # its inputs (a.txt/b.txt) and blocked.  Invariants preserved: writers
            # are still each isolated WHILE in-flight (the merge runs only after a
            # writer completes, no longer racing), writes stay confined to the
            # clone, and the merge stays conflict-free by the disjointness gate.
            #
            # CRITICAL: the merge can RAISE (a conflict = breached-disjointness
            # invariant, or any git failure).  We wrap it in its own try/finally so
            # that — pass or fail — this task is STILL marked terminal + notified
            # below.  Otherwise a raised merge would strand the admission loop on
            # `progress.wait()` forever (the coroutine never signalling completion).
            # The exception is re-raised after the terminal transition so the
            # post-loop drain surfaces it (and runs the clean-up + re-raise path).
            try:
                if done_wt is not None:
                    # Both merge and discard mutate the shared worktree admin dir /
                    # refs, so both run under `worktree_admin_lock` — serialized
                    # against every other writer's add/merge/discard (MAJOR-1).
                    async with worktree_admin_lock:
                        if succeeded:
                            await asyncio.to_thread(_merge_worktree, done_wt)
                        else:
                            # MINOR-1 (writer-failure): a FAILED writer's partial
                            # writes must NOT land in the base — DISCARD its worktree
                            # (remove, don't merge).
                            await asyncio.to_thread(_discard_worktree, done_wt)
            finally:
                # NOW mark terminal + notify — AFTER the merge has landed in HEAD
                # (on success), so a downstream admitted by this notify (its last
                # dep now terminal) carves its worktree from a HEAD that already
                # carries this writer's output.  This runs even if the merge raised,
                # so the admission loop is never stranded; the exception (if any)
                # still propagates out of the coroutine to the post-loop drain.
                async with progress:
                    results[tid] = result
                    terminal.add(tid)
                    in_flight.pop(tid, None)
                    if succeeded and jkey is not None:
                        key_tracker.record(tid, jkey)
                    progress.notify_all()

    # ── the admission loop ──────────────────────────────────────────────────
    spawned: set[str] = set()
    # Strong refs to the running coroutines so they are not GC'd mid-flight
    # (asyncio holds only a weak ref to a bare ensure_future task).  `running` is
    # the LIVE in-flight set (drained by the done-callback); `all_workers` is a
    # PERSISTENT list of every worker ever spawned, so the post-loop drain can
    # await ALL of them and read their results/exceptions directly from the
    # gather return — a worker that RAISES from inside `_run_one` (an eager-merge
    # CONFLICT, BudgetExceeded, CloneEscapeError) is therefore never lost even
    # after the done-callback has discarded it from `running`.
    running: set[asyncio.Task[None]] = set()
    all_workers: list[asyncio.Task[None]] = []
    try:
        while len(terminal) < len(ordered):
            async with progress:
                # Admit every currently-ready, not-yet-spawned task
                # (deterministic order).  Independence is evaluated against the
                # LIVE in-flight set, incrementally including peers admitted in
                # this same pass.
                admitted_this_pass: list[Mapping[str, Any]] = []
                for task in ordered:
                    tid = _task_id(task)
                    if tid in spawned:
                        continue
                    # `live` = currently in-flight + peers admitted this pass.
                    live = dict(in_flight)
                    for adm in admitted_this_pass:
                        live[_task_id(adm)] = adm
                    if _ready(task, live):
                        admitted_this_pass.append(task)
                for task in admitted_this_pass:
                    tid = _task_id(task)
                    spawned.add(tid)
                    in_flight[tid] = task
                if admitted_this_pass:
                    for task in admitted_this_pass:
                        coro_task = asyncio.ensure_future(_run_one(task))
                        running.add(coro_task)
                        all_workers.append(coro_task)
                        coro_task.add_done_callback(running.discard)
                elif in_flight:
                    # Nothing newly admittable but work is in flight — wait for a
                    # completion to change the picture.
                    await progress.wait()
                else:
                    # Deadlock guard: nothing ready, nothing in flight, not all
                    # terminal.  Only possible on a malformed DAG that
                    # validate_dag would have rejected; fail loud, never hang.
                    remaining = [t for t in ordered if _task_id(t) not in terminal]
                    raise RuntimeError(
                        "pipeline stalled: no ready task and none in flight, but "
                        f"{[_task_id(t) for t in remaining]} are not terminal — "
                        "malformed DAG (should have failed validate_dag)."
                    )
    except BaseException:
        # The admission loop itself raised (e.g. the deadlock guard).  Drain any
        # in-flight workers (best-effort, never masking the loop's exception),
        # restore a clean base + remove leftover worktrees, then re-raise.
        with contextlib.suppress(BaseException):
            if all_workers:
                await asyncio.gather(*all_workers, return_exceptions=True)
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(
                _cleanup_after_merge_failure, clone_dir, list(writer_worktrees.values())
            )
        raise
    else:
        # Loop completed normally (every task terminal).  Await EVERY worker and
        # read their results DIRECTLY from the gather return (not via the
        # done-callback, whose `call_soon` scheduling may not have fired yet), then
        # surface the FIRST exception any worker raised — a merge CONFLICT
        # (breached-disjointness invariant), BudgetExceeded, or CloneEscapeError.
        if all_workers:
            outcomes = await asyncio.gather(*all_workers, return_exceptions=True)
            first_exc = next((o for o in outcomes if isinstance(o, BaseException)), None)
            if first_exc is not None:
                # MAJOR-2 (eager-merge variant): merges land eagerly into the base,
                # so a mid-flight failure can leave the base half-merged/dirty AND
                # leave still-in-flight writers' worktrees on disk.  Restore a CLEAN
                # base + remove every leftover worktree so a caller catching the
                # error to fall back never inherits a polluted clone, THEN re-raise
                # the original exception (the loud INVARIANT VIOLATION is preserved,
                # never auto-resolved).
                await asyncio.to_thread(
                    _cleanup_after_merge_failure, clone_dir, list(writer_worktrees.values())
                )
                raise first_exc

    # All successful writers were merged back EAGERLY (in completion order) as
    # each became terminal — see the eager-merge step in `_run_one`.  By the time
    # we get here the base clone already carries every merged writer's output and
    # `writer_worktrees` is drained, so there is no deferred merge pass.
    return [results[_task_id(t)] for t in ordered]


def _attempt_for(task: Mapping[str, Any]) -> int:
    """The attempt number for this dispatch.  The journal key is attempt-free
    (a retry of the same inputs replays), so the scheduler dispatches each task
    once at attempt 1 in the barrier-free path — the engine's re-queue site
    (``parallel``) owns multi-attempt retries.  Honors a pre-set ``attempts``
    count if the caller threads one (deterministic; no clock)."""
    return int(task.get("attempts") or 0) + 1


def _seed_per_agent(
    tasks: Sequence[Mapping[str, Any]],
    model_for: Callable[[Mapping[str, Any], int], str],
    est_for: Callable[[str], int],
) -> int:
    """Conservative per-agent token seed for ``static_fleet_width``: the MAX
    per-agent estimate across the task set (so fleet-width never over-provisions
    relative to the priciest task).  At least 1 to avoid a zero divisor."""
    if not tasks:
        return 1
    ests = [est_for(model_for(t, _attempt_for(t))) for t in tasks]
    return max(1, max(ests))
