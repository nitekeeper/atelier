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
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.budget_pool import BudgetExceeded, BudgetPool, format_usage_report
from scripts.cli_dispatch import (
    DEFAULT_ALLOWED_TOOLS as _DEFAULT_ALLOWED_TOOLS,
)
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
    native_sandbox_wrap,
    real_cli_runner,
    run_attempt,
)
from scripts.dag import DagProof
from scripts.git_utils import git as _git
from scripts.pm_dispatch import (
    MAX_ATTEMPTS,
    MAX_PARALLEL_WORKERS,
    WALL_CLOCK_S,
    WaveDispatcher,
    _default_escalate,
    _parse_abandon_category,
)
from scripts.result_journal import ResultJournal

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_PARALLEL_WORKERS",
    "JournalKeyTracker",
    "WorktreeError",
    "is_abandoned_result",
    "parallel",
    "pipeline",
    "run_host_pipeline_for_project",
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


def _missing_declared_writes(task: Mapping[str, Any], write_dir: str | Path) -> list[str]:
    """Return the declared ``writes`` (repo-relative) that show NO CHANGE vs ``HEAD``
    in *write_dir* — the git working tree the agent actually wrote into (the task's
    worktree when isolated, else the clone).

    BUG4b: a plain ``.exists()`` check is NOT sufficient. Every declared write
    typically ALREADY EXISTS as a committed file at ``HEAD`` (the task EDITS an
    existing file), so a no-op `done` — a worker that declared `writes` but changed
    nothing — would have its declared path STILL exist and slip through the
    false-`done` guard. So the contract is a HEAD-RELATIVE DIFF: a declared write
    must actually show a change vs ``HEAD`` in its working tree (a new untracked
    file, a staged/unstaged modification, or a delete). A declared write that is
    byte-identical to ``HEAD`` (or absent at both) is reported as "missing" so the
    no-op `done` is correctly flagged.

    The declared ``writes`` are repo-relative paths (the same vocabulary the DAG
    write-disjointness gate uses). We ask git ONCE for the full set of changed
    paths via ``git status --porcelain`` in *write_dir* and intersect with the
    declared set — a path with NO porcelain entry changed nothing vs HEAD and is
    flagged. Content is deliberately NOT over-constrained beyond "differs from
    HEAD" (a legitimately-empty-but-new file still shows as untracked, so it is
    accepted). Returns the missing entries in declaration-stable (sorted) order; an
    empty list means every declared write actually changed vs HEAD.
    """
    declared = sorted(_writes(task))
    if not declared:
        return []
    changed = _changed_paths_vs_head(write_dir)
    return [w for w in declared if w not in changed]


def _changed_paths_vs_head(write_dir: str | Path) -> set[str]:
    """Return the set of repo-relative paths that differ from ``HEAD`` in the git
    working tree *write_dir* (modified, added, deleted, renamed, or untracked).

    Parses ``git status --porcelain`` (the stable, scriptable form): each entry's
    path component is collected. A rename ``R old -> new`` contributes BOTH the new
    AND the old path (either side is a genuine change touching a declared write).
    Quoted paths (git quotes names with special chars under the default
    ``core.quotePath``) are passed through verbatim; declared ``writes`` are plain
    repo-relative slugs in practice, so the comparison is exact. Best-effort: a git
    failure (not a repo / detached oddity) returns an empty set, which fail-CLOSED
    flags every declared write as unchanged (a `done` is rejected rather than
    wrongly accepted)."""
    res = _git(["status", "--porcelain"], Path(write_dir), check=False)
    if res.returncode != 0:
        return set()
    changed: set[str] = set()
    for line in res.stdout.splitlines():
        if not line:
            continue
        # Porcelain v1: "XY <path>" or "XY <old> -> <new>" for renames/copies.
        entry = line[3:] if len(line) > 3 else line.strip()
        if " -> " in entry:
            old, new = entry.split(" -> ", 1)
            changed.add(old.strip())
            changed.add(new.strip())
        else:
            changed.add(entry.strip())
    return changed


def _sandbox_wrap_for_write_root(
    base_wrap: SandboxWrap,
    write_root: str | Path,
) -> SandboxWrap:
    """Return the ``sandbox_wrap`` to use for a dispatch whose writes land in
    *write_root* (the carved worktree for an isolated writer, else the clone).

    BUG4a: a writer runs in a per-writer git worktree under
    ``clone_dir/.atelier-worktrees/<id>``, NOT the clone root, so its OS sandbox
    must confine writes to (and land them in) ITS worktree. The ``sandbox_wrap``
    threaded into :func:`pipeline` is a single closure built ONCE for the clone, so
    when it is a NATIVE sandbox (tagged by :func:`~scripts.cli_dispatch.native_sandbox_wrap`)
    we REBUILD it here with ``write_root`` so the ``allowWrite`` set tracks the
    worktree. A non-native wrap (identity, a custom external wrapper, or an already
    worktree-scoped one) is returned UNCHANGED — only a clone-scoped native sandbox
    is re-pointed, and only when *write_root* actually differs from its current
    confinement.
    """
    clone_dir = getattr(base_wrap, "native_clone_dir", None)
    if clone_dir is None:
        # Not a native sandbox (identity / external / custom) — leave it as-is.
        return base_wrap
    target = str(Path(write_root).resolve())
    if getattr(base_wrap, "native_write_root", None) == target:
        # Already confined to this exact write root (e.g. no worktree carved →
        # write_root IS the clone) — no rebuild needed.
        return base_wrap
    return native_sandbox_wrap(clone_dir, write_root=target)


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


# ── M5: termination/cascade/escalation parity helpers (Path A → Path B) ──────

#: The structured "this task was abandoned" status string stored in
#: ``results[tid]`` (NOT a bare ``None``, NOT the ``_FailedAttempt`` sentinel —
#: which means an attempt RAN and failed). A downstream cascade reads this status,
#: so the shape MUST be stable.
_ABANDONED_STATUS = "abandoned"

#: Abandon categories — IDENTICAL strings to Path A (pm_dispatch.py): ``"blocked"``
#: for a cascade (dependent of an abandoned upstream) and ``"capacity"`` for a
#: budget exhaustion (BudgetExceeded). Reusing the same vocabulary keeps the two
#: scheduler paths' abandonment reports interchangeable.
_CATEGORY_BLOCKED = "blocked"
_CATEGORY_CAPACITY = "capacity"

#: PRIVATE engine-controlled marker stamped on EVERY :func:`_abandoned_result`
#: dict. THIS path fully controls this key; it is the positive signal that a
#: result is the engine's OWN structured abandon (capacity/cascade) rather than a
#: worker-authored self-abandon envelope. Worker envelope content is UNTRUSTED
#: DATA (CLAUDE.md boundary) and ``ENVELOPE_SCHEMA`` sets ``additionalProperties:
#: True``, so a worker CAN forge arbitrary keys (incl. ``category`` /
#: ``upstream_task_id`` / even this sentinel) — therefore the sentinel is paired
#: with a schema-guaranteed negative check (``type is None``) in
#: :func:`_is_structured_abandon` so the disambiguation is un-spoofable.
_ENGINE_ABANDON_KEY = "_engine_abandon"


def _abandoned_result(
    task_id: str,
    *,
    category: str,
    upstream_task_id: str | None,
    last_status: str,
) -> dict[str, Any]:
    """Build the STRUCTURED abandoned result dict stored in ``results[tid]``.

    Distinct from a bare ``None`` (the old non-success marker) and from the
    ``_FailedAttempt`` sentinel (which means an attempt ran and failed): this dict
    asserts the task was abandoned WITHOUT (cascade) or because of (capacity) an
    attempt, naming the upstream that caused a cascade. Downstream cascade
    resolution and any journal-key path key on ``status == "abandoned"``, not on a
    missing journal key (an abandoned task has none).

    Carries the engine-controlled :data:`_ENGINE_ABANDON_KEY` sentinel (and NO
    ``type`` key) so :func:`_is_structured_abandon` can tell this engine dict apart
    from an UNTRUSTED worker self-abandon envelope un-spoofably.
    """
    return {
        _ENGINE_ABANDON_KEY: True,
        "status": _ABANDONED_STATUS,
        "category": category,
        "upstream_task_id": upstream_task_id,
        "task_id": task_id,
        "last_status": last_status,
    }


def is_abandoned_result(result: Any) -> bool:
    """True iff *result* has ``status == "abandoned"`` — covers BOTH this path's
    own STRUCTURED abandon (cascade/capacity) AND a worker-authored self-abandon
    envelope. Both are terminal abandonments that must cascade to dependents, so
    for the cascade-source decision they are treated alike.

    To tell them APART (e.g. to choose the right escalation category — this path's
    dicts carry an explicit ``category``, a worker envelope's category lives in
    ``notes_md`` line 1), use :func:`_is_structured_abandon`.
    """
    return isinstance(result, Mapping) and result.get("status") == _ABANDONED_STATUS


def _is_structured_abandon(result: Any) -> bool:
    """True iff *result* is one of THIS path's OWN structured abandon dicts
    (built by :func:`_abandoned_result`) — distinguished from an UNTRUSTED
    worker-authored self-abandon envelope un-spoofably.

    SECURITY (E2-R2): worker envelope content is untrusted DATA (CLAUDE.md
    boundary) and ``ENVELOPE_SCHEMA`` sets ``additionalProperties: True``, so
    ``validate_envelope`` KEEPS any keys a worker forges — a worker CAN add
    ``category`` / ``upstream_task_id`` (and even :data:`_ENGINE_ABANDON_KEY`) to
    its envelope. So we do NOT key on the presence of those (spoofable) keys.
    Instead we combine an engine-controlled POSITIVE marker with a
    schema-guaranteed NEGATIVE one:

      * :data:`_ENGINE_ABANDON_KEY` is True — stamped by :func:`_abandoned_result`;
      * ``result.get("type") is None`` — the engine dict has NO ``type`` key,
        whereas EVERY validated worker envelope ALWAYS has ``type == "task_result"``
        (``ENVELOPE_SCHEMA`` constrains ``type`` to ``const: "task_result"`` AND
        lists it in ``required``, so validation rejects any worker result lacking
        it / carrying a different value).

    The AND is airtight: a worker would have to BOTH forge the sentinel AND drop
    its mandatory ``type`` field — but a result with no/other ``type`` never passes
    ``validate_envelope``, so it never reaches here as a worker result. A worker
    self-abandon therefore always evaluates False and is routed to the
    ``notes_md``-parsed, TM-006-grammar-checked category (FIX 1), never its
    forged ``category``/``upstream_task_id``.
    """
    return (
        isinstance(result, Mapping)
        and result.get(_ENGINE_ABANDON_KEY) is True
        and result.get("type") is None
    )


def _result_is_success(result: Any) -> bool:
    """True iff *result* is a GENUINE success that produced its outputs — the ONLY
    thing that satisfies a downstream dependency (M5 change 2, the classifier).

    Success ⟺ a validated envelope ``Mapping`` whose ``status == "done"``. EVERY
    other terminal encoding is a non-success that must cascade to dependents:

      (i)   the ``_FailedAttempt`` sentinel — an attempt ran and failed (CLI
            ``is_error`` / non-zero exit / wall-clock timeout / runner error);
      (ii)  this path's STRUCTURED abandoned envelope (cascade ``"blocked"`` /
            capacity ``"capacity"`` — including the BudgetExceeded → capacity case);
      (iii) the false-`done`-converted-to-FAILED_ATTEMPT case (the #120 guard — a
            `done` whose declared writes were absent becomes ``FAILED_ATTEMPT``,
            covered by (i));
      (iv)  a worker-authored validated envelope whose ``status`` is a terminal- or
            non-terminal-FAILURE (``"failed"`` / ``"abandoned"`` / ``"blocked"`` /
            ``"needs-input"``) — parity with Path A, which routes a ``failed`` /
            ``abandoned`` envelope through ``_abandon_and_escalate`` and adds it to
            ``abandoned_ids`` (pm_dispatch.py:802-821); only ``done`` produced the
            declared outputs a dependent reads;
      (v)   a bare ``None`` left by a legacy non-success path (defensive
            fail-closed — a terminal upstream with no validated envelope).

    Missing ANY of these would let a dependent be admitted on bad inputs — silent
    corruption. This single predicate is the source of truth for BOTH the
    ``succeeded`` decision (journal-key recording) and the ``pipeline_abandoned``
    cascade-source population.
    """
    return isinstance(result, Mapping) and result.get("status") == "done"


def _failed_envelope_category(result: Any) -> str | None:
    """For a worker-authored validated envelope that is NOT a success, return the
    abandon CATEGORY to escalate under (parity with Path A), or None if *result*
    is not such an envelope.

    * ``status == "abandoned"`` (worker self-abandon) → the category PARSED from
      ``notes_md`` line 1 via :func:`~scripts.pm_dispatch._parse_abandon_category`
      (the TM-006 ABANDON_RE grammar token: ``scope|blocked|conflict|capacity|
      stale_rules|no_consensus|destructive_rejected|tests_unrecoverable``) —
      EXACTLY Path A (pm_dispatch.py:793). Emitting the literal ``"abandoned"``
      would be an OUT-OF-GRAMMAR token, so we never do that.
    * ``status == "failed"`` → ``"failed"`` (Path A pm_dispatch.py:816).
    * non-terminal ``"blocked"`` / ``"needs-input"`` returned at dispatch (pipeline
      dispatches once, so there is no retry to consume) → its own status name, so
      the abandonment is never silent.

    This path's OWN structured abandon dicts (carry ``category`` + ``upstream_task_id``)
    are escalated at their dedicated capacity/cascade sites, NOT here — guard against
    them with :func:`_is_structured_abandon` so a future branch-order change can't
    feed one through this helper.
    """
    if not isinstance(result, Mapping):
        return None
    if _is_structured_abandon(result):
        # This path's own structured abandoned envelope — escalated elsewhere.
        return None
    status = result.get("status")
    if status == "abandoned":
        # Worker self-abandon — parse the real TM-006 category from notes_md
        # (Path A parity), never the literal out-of-grammar "abandoned".
        return _parse_abandon_category(result.get("notes_md") or "")
    if status in ("failed", "blocked", "needs-input"):
        return str(status)
    return None


def _reviewer_verdict(result: Any, succeeded: bool) -> str:
    """Classify a reviewer dispatch result into the review-fix loop verdict
    (M6b-1): ``"pass"`` / ``"block"`` / ``"abandon"``.

    A reviewer is just another worker — its envelope IS the verdict:

      * ``"pass"`` — a validated ``done`` envelope (``succeeded`` True per
        :func:`_result_is_success`). The implement output is clean; the loop ends.
      * ``"block"`` — a deliberate BLOCKING VERDICT the reviewer authored:
        ``status == "blocked"`` or ``"needs-input"``. The reviewer ran fine and
        found issues; the implement is re-dispatched to FIX.
      * ``"abandon"`` — a reviewer WORKER FAILURE, NOT a verdict: a
        ``_FailedAttempt`` sentinel (CLI error / timeout), this path's structured
        capacity-abandon (the child budget tripped), or a worker-authored
        ``failed`` / ``abandoned`` envelope. The implement is NOT re-dispatched (a
        broken reviewer cannot grade); the reviewer cascades to ITS dependents.

    The block-vs-abandon split is load-bearing (T5): a blocking VERDICT triggers
    a fix; an abandoning reviewer does not. A bare ``done`` is the ONLY pass.
    """
    if succeeded and _result_is_success(result):
        return "pass"
    if isinstance(result, Mapping) and not _is_structured_abandon(result):
        status = result.get("status")
        if status in ("blocked", "needs-input"):
            return "block"
    return "abandon"


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


def _porcelain_paths(porcelain: str) -> list[str]:
    """Extract the changed paths from ``git status --porcelain`` output.

    Each porcelain line is ``XY <path>`` (a 2-char status code, a space, then the
    path; a rename is ``orig -> new``).  Returns the affected paths so a merge
    diagnosis can NAME the dirty base files.  Best-effort + non-raising — used
    only to enrich an error message.
    """
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        # Drop the 2-char status code + the separating space.
        rest = line[3:] if len(line) > 3 else line.strip()
        # A rename/copy renders as "old -> new"; name the destination path.
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.append(rest)
    return sorted(paths)


def _merge_worktree(wt: _Worktree) -> None:
    """Commit a writer worktree's changes and merge its branch into the base
    clone, then remove the worktree.  Conflict-free by construction (disjoint
    writes); a conflict here is surfaced loudly, never silently resolved.  The
    diagnosis distinguishes two causes: a SCHEDULER bug (the disjointness
    invariant was violated — base was clean) versus an ENVIRONMENT fault (the
    base clone was already dirty before the merge).  Both name the conflicting
    paths so the downstream debugger is not sent astray.
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

    # Snapshot the base clone's cleanliness IMMEDIATELY before the merge.  A
    # merge conflict has two distinct causes and the diagnosis below depends on
    # telling them apart: (a) a SCHEDULER bug — two concurrent writers wrote the
    # same path (disjointness breach); (b) an ENVIRONMENT fault — the base clone
    # already had uncommitted/untracked changes that collide with the
    # worktree's.  Capturing here (not after the failed merge) keeps the signal
    # uncontaminated by merge side effects.
    pre_status = _git(["status", "--porcelain"], base, check=False)
    # Linked worktrees physically live under ``.atelier-worktrees/`` INSIDE the
    # base clone, so ``git status`` reports them — that is engine scaffolding,
    # not operator dirt, and must NOT count toward "base was dirty".  Filter it.
    base_dirty_paths = [
        p for p in _porcelain_paths(pre_status.stdout) if not p.startswith(".atelier-worktrees/")
    ]

    # Merge into the base clone's current branch.  --no-ff keeps an explicit
    # merge record; disjoint writes mean no conflict.  --no-ff CREATES a merge
    # commit, so this also needs the engine identity (same fixed `-c user.*`).
    merge = _git(
        _identity_commit_args(["merge", "--no-ff", wt.branch, "-m", f"Merge {wt.branch}"]),
        base,
        check=False,
    )
    if merge.returncode != 0:
        # Gather the unmerged (conflicting) paths BEFORE aborting — `--abort`
        # tears down the conflict state, so this must run first.
        conflict = _git(["diff", "--name-only", "--diff-filter=U"], base, check=False)
        conflict_paths = sorted(p for p in (ln.strip() for ln in conflict.stdout.splitlines()) if p)
        _git(["merge", "--abort"], base, check=False)
        conflict_str = ", ".join(conflict_paths[:20]) or "<none reported>"
        git_tail = merge.stderr.strip()[:200]
        if base_dirty_paths:
            # ENVIRONMENT fault, NOT a scheduler bug: the base clone was dirty.
            dirty_str = ", ".join(base_dirty_paths[:20])
            raise WorktreeError(
                f"merging worktree {wt.branch!r} CONFLICTED because the base "
                "clone had UNCOMMITTED/UNTRACKED changes before the merge "
                "(this is an unexpected-precondition / environment fault, NOT a "
                f"disjointness-invariant breach). dirty base paths: {dirty_str}; "
                f"conflicting paths: {conflict_str}. git: {git_tail}"
            )
        raise WorktreeError(
            f"INVARIANT VIOLATION: merging worktree {wt.branch!r} CONFLICTED — "
            "two concurrently-isolated writers must be write-disjoint by "
            "construction (gate 3 + dynamic re-check), so a conflict means the "
            f"disjointness invariant was breached. conflicting paths: "
            f"{conflict_str}. git: {git_tail}"
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
    allowed_tools: Sequence[str] | None = _DEFAULT_ALLOWED_TOOLS,
    sandbox_wrap: SandboxWrap = identity_sandbox_wrap,
    escalate_fn: Callable[[Mapping[str, Any]], None] | None = None,
    max_budget_usd: float | None = None,
    review_pairing: Mapping[str, str] | None = None,
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

    **M5 — termination/cascade/escalation parity (Path A → Path B).**

    * ``escalate_fn`` — the GUARANTEED-emitting escalation sink (parity with
      :class:`~scripts.pm_dispatch.WaveDispatcher`). Defaults to
      :func:`~scripts.pm_dispatch._default_escalate` (always logs a WARNING; never
      silent). When ``pipeline()`` is composed in a production root, thread the
      SAME escalate_fn used for the WaveDispatcher there. A task abandoned by
      cascade (``"blocked"``) or budget exhaustion (``"capacity"``) is escalated
      through this sink — never best-effort.
    * **Cascade-abandon (the crux).** A not-yet-spawned task with an abandoned
      ancestor is NEVER admitted: it is marked terminal with a structured abandoned
      result (``category="blocked"``, naming the upstream), charges NO attempt, and
      escalates. The cascade is TRANSITIVE — each cascade-abandoned task is itself
      added to the abandoned set so its own descendants cascade too (parity with
      ``pm_dispatch._first_abandoned_ancestor`` /
      ``WaveDispatcher.abandoned_ids``).
    * **Budget exhaustion is per-task, not whole-run.** A ``BudgetExceeded`` raised
      pre-spawn for one task abandons+escalates THAT task (``category="capacity"``)
      and lets its dependents cascade, while unrelated independent tasks still
      complete. It does NOT abort the run.
    * ``max_budget_usd`` — optional per-task ``claude --max-budget-usd`` dollar
      ceiling threaded into ``run_attempt`` (the documented second hung-query kill
      lever). ``None`` ⇒ ``run_attempt`` derives it from the per-task token estimate.

    **Intentional divergences from Path A (WaveDispatcher) — NOT bugs.** Two
    deliberate decisions a future maintainer should not "fix" without re-scoping:

    1. A terminal ``FAILED_ATTEMPT`` task with NO dependents does NOT self-escalate
       here, whereas Path A escalates a task on attempt-exhaustion. In the
       barrier-free path a bare ``FAILED_ATTEMPT`` is the engine's normal failure
       marker; it stays observable in the returned results list and escalates only
       when a DEPENDENT cascades (category ``"blocked"``). Full self-escalation
       parity (escalate a terminal ``FAILED_ATTEMPT`` as ``"capacity"`` once the
       attempt budget is spent) is a DEFERRED FOLLOW-UP, intentionally out of M5's
       plan scope (M5 scope = cascade-abandon + per-task ``BudgetExceeded`` +
       transient-spawn retry + wall-clock). A worker-AUTHORED ``failed`` /
       ``abandoned`` envelope DOES self-escalate here (Path-A-parity category).
    2. Worker ``blocked`` / ``needs-input`` envelopes are TERMINAL-and-cascade in
       this path (it has NO re-queue site by design — each task is dispatched once),
       whereas Path A treats them as RETRYABLE. Single-dispatch makes
       terminal-and-cascade the only safe option (admitting a dependent on a
       ``blocked`` upstream's absent output is silent corruption); it matches the
       M5 obligation and is NOT a regression.
    """
    ordered = sorted(tasks, key=_sort_key)
    # Normalize the None-default pairing (avoid a mutable default arg; parity with
    # the None pattern in run_host_pipeline_for_project + CliDispatchTools).
    review_pairing = review_pairing or {}
    key_tracker = JournalKeyTracker(journal)
    # GUARANTEED-emitting escalation sink (parity with WaveDispatcher). Never a
    # best-effort default — _default_escalate always logs a WARNING.
    _escalate = escalate_fn if escalate_fn is not None else _default_escalate
    # In-memory task index for the bounded ancestor BFS (parity with
    # WaveDispatcher._task_index): task_id → task dict, for walking depends_on.
    task_index: dict[str, Mapping[str, Any]] = {_task_id(t): t for t in ordered}
    # M6b-1 — the review-fix loop pairing. ``review_pairing`` is the BIDIRECTIONAL
    # implement↔review task-id map (planner.build_review_pairing): it carries each
    # pair twice (``impl_id -> review_id`` AND ``review_id -> impl_id``). Split it
    # into the two ORIENTED single-direction indexes the loop reads:
    #   * review_by_implement[impl_id] = review_id — an IMPLEMENT task that has a
    #     paired reviewer drives the nested review-fix loop after it succeeds.
    #   * implement_by_review[review_id] = impl_id — a REVIEW task is driven by its
    #     implement (NOT admitted independently) and resolves the reviewed task for
    #     the dispatch-time disjointness re-check.
    # Orientation comes from the in-memory tasks' own ``reviews`` field (the SAME
    # source build_review_pairing read — the review task carries ``reviews``, the
    # implement does not), and a pair is ACTIVATED only when BOTH directions are
    # present in ``review_pairing`` (the caller's explicit opt-in) AND both endpoints
    # are in this task set (defense in depth — a pairing pointing outside the
    # dispatched set is ignored). Empty ``review_pairing`` ⇒ both maps empty ⇒
    # byte-for-byte the pre-M6b-1 single-dispatch behavior.
    review_by_implement: dict[str, str] = {}
    implement_by_review: dict[str, str] = {}
    # Lazy import (parity with the compute_dag_proof import in the host entrypoint):
    # the shared disjointness comparator is used ONLY when a pairing is active.
    from scripts.planner import assert_reviewer_disjoint as _assert_reviewer_disjoint

    for t in ordered:
        reviews = t.get("reviews")
        if not isinstance(reviews, str) or not reviews:
            continue
        review_id = _task_id(t)
        impl_id = reviews
        if (
            review_id in task_index
            and impl_id in task_index
            and review_pairing.get(impl_id) == review_id
            and review_pairing.get(review_id) == impl_id
        ):
            review_by_implement[impl_id] = review_id
            implement_by_review[review_id] = impl_id
    # The cascade-abandon SOURCE set — task_ids that ended abandoned (failed
    # attempt, budget capacity, or cascade blocked). Parity with
    # WaveDispatcher.abandoned_ids; accumulated as each task finalizes so the
    # cascade is transitive. Mutated only under the `progress` lock.
    pipeline_abandoned: set[str] = set()

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

    def _first_abandoned_ancestor(task: Mapping[str, Any]) -> str | None:
        """Return the first ancestor id in ``pipeline_abandoned`` reachable via
        ``depends_on``, or None (M5 — parity with
        ``pm_dispatch._first_abandoned_ancestor``).

        Bounded BFS with a visited-set so a cyclic ``depends_on`` (malformed
        planner output) terminates. The walk reads the in-memory ``depends_on``
        edges via ``task_index`` and checks each dep against ``pipeline_abandoned``;
        because cascade-abandoned tasks are themselves added to
        ``pipeline_abandoned``, this naturally finds a TRANSITIVE abandoned
        ancestor (the dependent of a cascade-abandoned task cascades too). Caller
        holds the ``progress`` lock, so the ``pipeline_abandoned`` read is
        consistent.
        """
        visited: set[str] = set()
        frontier: list[str] = list(_depends_on(task))
        while frontier:
            dep_id = frontier.pop()
            if dep_id in visited:
                continue
            visited.add(dep_id)
            if dep_id in pipeline_abandoned:
                return dep_id
            dep_task = task_index.get(dep_id)
            if dep_task is not None:
                frontier.extend(_depends_on(dep_task))
        return None

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

    async def _dispatch_one_attempt(
        the_task: Mapping[str, Any],
        attempt: int,
        the_budget: BudgetPool,
        *,
        extra_upstream_hashes: Sequence[str] = (),
    ) -> tuple[Any, str | None, bool]:
        """Dispatch ONE attempt of *the_task* and return ``(result, jkey, succeeded)``.

        Encapsulates the per-attempt mechanics SHARED by the main dispatch and the
        M6b-1 nested review-fix loop: the seam calls, the SHARED ``semaphore``
        (NEVER a second one — the global concurrency cap is one counter), the
        optional per-writer worktree carve, ``run_attempt`` charged against
        *the_budget* (the parent pool for the first attempt, a CHILD pool for the
        fix loop — charges bubble to the parent), the false-`done` guard, and the
        EAGER worktree merge-on-success / discard-on-fail (so TERMINAL ⟹ MERGED is
        preserved for downstreams). ``BudgetExceeded`` is converted to a structured
        capacity-abandon result (M5 per-task semantics) — it does NOT propagate.

        ``extra_upstream_hashes`` is APPENDED to the DAG-derived
        ``upstream_envelope_hashes`` (M6b-1): the journal key is content-addressed
        and IGNORES ``attempt`` (a fresh attempt with identical inputs REPLAYS), so
        a fix re-dispatch makes genuine progress ONLY when its INPUTS differ — the
        review-fix loop threads the reviewer's BLOCKING-envelope hash here so the
        FIX is a content-genuinely-different dispatch ("implement, GIVEN the
        reviewer's feedback") that re-runs rather than replaying the blocked output.

        Does NOT touch ``terminal`` / ``results`` / ``in_flight`` /
        ``pipeline_abandoned`` / escalation — that finalization is the caller's job
        (:func:`_finalize_task`), so the loop can drive several attempts before one
        terminal transition.
        """
        the_tid = _task_id(the_task)
        result: Any = None
        jkey: str | None = None
        success = False
        done_wt: _Worktree | None = None
        carve_failed = False
        # OUTER try/finally so the EAGER merge/discard tail ALWAYS runs — even when a
        # non-BudgetExceeded exception (CloneEscapeError, an eager-merge CONFLICT, …)
        # propagates out of the dispatch — so a worktree is never leaked
        # un-merged/un-discarded (parity with the pre-M6b-1 `finally`). The raise
        # still propagates to `_run_one`'s except (terminal + notify + re-raise).
        try:
            # MAJOR-1: the production seams are FALLIBLE — they run inside the try so a
            # raise routes through the merge/cleanup tail instead of leaking a
            # worktree (the caller's finalize still marks terminal + notifies).
            model = model_for(the_task, attempt)
            briefing = briefing_for(the_task, attempt)
            async with semaphore:
                run_cwd: str | Path = clone_dir
                run_add_dir: str | Path = clone_dir
                if worktree_factory is not None and _is_writer(the_task):
                    # Carve the worktree under the worktree-admin lock so the
                    # `.git/worktrees/` admin mutation is serialized against every
                    # other writer's add/merge/remove (the slow agent run below stays
                    # concurrent — only this fast git step is locked).
                    try:
                        async with worktree_admin_lock:
                            done_wt = await asyncio.to_thread(worktree_factory, the_tid)
                    except WorktreeError:
                        # SAFE-BY-CONSTRUCTION: isolation is mandatory for a writer
                        # under fan-out; FAIL THIS ATTEMPT (no worktree ⇒ no merge ⇒
                        # nothing lands in the base) rather than run un-isolated.
                        result = FAILED_ATTEMPT
                        carve_failed = True
                    else:
                        run_cwd = done_wt.path
                        run_add_dir = done_wt.path
                        async with lock:
                            writer_worktrees[the_tid] = done_wt
                if not carve_failed:
                    up_hashes = list(
                        scheduler_upstream_hashes_for(the_task, dag_proof, key_tracker)
                    )
                    # M6b-1: APPEND the fix-feedback hashes (the reviewer's blocking
                    # envelope) so a fix re-dispatch is content-genuinely-different
                    # from the prior attempt (attempt alone is NOT in the journal key).
                    up_hashes.extend(extra_upstream_hashes)
                    jkey = journal.key(
                        the_task,
                        attempt,
                        model=model,
                        briefing=briefing,
                        upstream_envelope_hashes=up_hashes,
                    )
                    # A journal HIT means run_attempt will REPLAY a previously-validated
                    # result at $0 WITHOUT re-executing — so the worktree is not
                    # re-materialized and the declared writes legitimately already live
                    # in HEAD (merged by the original run), unchanged. The false-`done`
                    # guard below (a diff-vs-HEAD check) is for catching a LYING FRESH
                    # `done`, not for re-checking a replay; skip it on a hit.
                    was_journal_hit = journal.lookup(jkey) is not None
                    # BUG4a: confine a native OS sandbox's writes to the dir the
                    # agent actually writes into — the carved worktree for an
                    # isolated writer, else the clone (`run_cwd` is whichever). A
                    # non-native / identity wrap is returned unchanged.
                    eff_sandbox_wrap = _sandbox_wrap_for_write_root(sandbox_wrap, run_cwd)
                    result = await run_attempt(
                        the_task,
                        attempt,
                        budget=the_budget,
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
                        sandbox_wrap=eff_sandbox_wrap,
                        max_budget_usd=max_budget_usd,
                    )
                    # FALSE-`done` GUARD (engine-level): a `done` writer that produced
                    # NONE of its declared outputs is converted to FAILED_ATTEMPT so
                    # the worktree is DISCARDED (its partial state never lands in the
                    # base). Read-only / review tasks (no declared `writes`) are EXEMPT.
                    if (
                        not is_failed_attempt(result)
                        and isinstance(result, Mapping)
                        and result.get("status") == "done"
                        and _is_writer(the_task)
                        and not was_journal_hit
                    ):
                        missing = _missing_declared_writes(the_task, run_cwd)
                        if missing:
                            logging.getLogger(__name__).warning(
                                "false `done` REJECTED for task %s: declared write(s) %s "
                                "absent in %s — converting to FAILED_ATTEMPT (worktree "
                                "discarded, not merged); the engine will re-queue/abandon "
                                "per budget.",
                                the_tid,
                                missing,
                                run_cwd,
                            )
                            result = FAILED_ATTEMPT
                            # Invalidate the cached (rejected) envelope so a retry
                            # RE-EXECUTES rather than replaying this false-`done`:
                            # run_attempt journaled the validated `done` BEFORE this
                            # guard saw it, and `attempt` is not part of the journal
                            # key, so without this delete the next attempt would be a
                            # journal HIT (skipping the guard above) and resurrect the
                            # rejected `done` as success.
                            journal.delete(jkey)
                    success = True
        except BudgetExceeded as exc:
            # M5 (3): a per-task budget exhaustion is a PER-TASK abandon — NOT a
            # whole-run abort. A CHILD pool's `assert_can_dispatch` trips when the
            # snapshot of the parent's remaining budget is exhausted, so a tripped
            # parent budget aborts the fix loop here too. Convert to a structured
            # capacity-abandon (same category as Path A) and do NOT re-raise.
            logging.getLogger(__name__).warning(
                "task %s ABANDONED (capacity): budget exhausted pre-spawn (%s) — "
                "per-task abandon+escalate, run continues for independent tasks.",
                the_tid,
                exc,
            )
            result = _abandoned_result(
                the_tid,
                category=_CATEGORY_CAPACITY,
                upstream_task_id=None,
                last_status=f"budget-exceeded: {exc}",
            )
            success = False
        finally:
            # ── EAGER worktree merge (success) / discard (failure) ──────────
            # OUTSIDE the semaphore (so the blocking git op never holds a concurrency
            # slot) and serialized by `worktree_admin_lock`. TERMINAL ⟹
            # MERGED-INTO-HEAD is preserved: a successful writer's output is in base
            # HEAD before the caller marks it terminal (and before its paired reviewer
            # reads HEAD). Runs even when the body RAISED (the merge can itself raise a
            # CONFLICT — surfaced loudly via the re-raise of the original exception).
            succeeded = success and _result_is_success(result)
            async with progress:
                popped_wt = writer_worktrees.pop(the_tid, None)
            eff_wt = popped_wt if popped_wt is not None else done_wt
            if eff_wt is not None:
                async with worktree_admin_lock:
                    if succeeded:
                        await asyncio.to_thread(_merge_worktree, eff_wt)
                    else:
                        await asyncio.to_thread(_discard_worktree, eff_wt)
        return result, jkey, succeeded

    async def _finalize_task(
        the_task: Mapping[str, Any],
        result: Any,
        jkey: str | None,
        succeeded: bool,
    ) -> None:
        """Mark *the_task* terminal + notify + (on non-success) escalate.

        Single source of truth for the per-task terminal transition (M5): records
        the result, adds to ``terminal`` (and on success records the journal key),
        and on a non-success adds the task to ``pipeline_abandoned`` (cascade
        source) and emits AT MOST ONE self-escalation under the result's REAL
        category (structured capacity, or the worker-authored envelope category) —
        OUTSIDE the lock. A bare ``_FailedAttempt`` / ``None`` self-escalates only
        when a dependent cascades (handled by the cascade gate), exactly as before.
        """
        the_tid = _task_id(the_task)
        escalation: dict[str, Any] | None = None
        async with progress:
            results[the_tid] = result
            terminal.add(the_tid)
            in_flight.pop(the_tid, None)
            if succeeded and jkey is not None:
                key_tracker.record(the_tid, jkey)
            elif not succeeded:
                pipeline_abandoned.add(the_tid)
                if _is_structured_abandon(result):
                    escalation = {
                        "kind": "escalation",
                        "task_id": the_tid,
                        "worker": the_task.get("assigned_to"),
                        "attempt": _attempt_for(the_task),
                        "category": result.get("category"),
                        "last_status": result.get("last_status"),
                        "upstream_task_id": result.get("upstream_task_id"),
                    }
                else:
                    env_category = _failed_envelope_category(result)
                    if env_category is not None:
                        escalation = {
                            "kind": "escalation",
                            "task_id": the_tid,
                            "worker": the_task.get("assigned_to"),
                            "attempt": _attempt_for(the_task),
                            "category": env_category,
                            "last_status": str(result.get("status")),
                            "upstream_task_id": None,
                        }
            progress.notify_all()
        # GUARANTEED escalation OUTSIDE the lock (the user callback must not run
        # while holding the admission lock).
        if escalation is not None:
            _escalate(escalation)

    async def _run_one(task: Mapping[str, Any]) -> None:
        # ``tid`` is a plain dict read the admission loop already performed to
        # spawn this task, so it cannot raise here; the ``finally`` keys on it.
        tid = _task_id(task)
        result: Any = None
        jkey: str | None = None
        succeeded = False
        try:
            # T6 — DISPATCH-TIME reviewer-disjointness re-check (M6b-1). When THIS
            # task is a paired REVIEWER, re-assert separation-of-duties via the SAME
            # comparator the planner used at synthesis (planner.assert_reviewer_disjoint
            # — one comparator, no drift). A persona re-assigned to the implementer's
            # AFTER synthesis is caught here. Fail-LOUD (raise) — routed through the
            # finalize tail below so the reviewer is marked terminal + notified and
            # the admission loop never hangs.
            if tid in implement_by_review:
                reviewed = task_index.get(implement_by_review[tid])
                if reviewed is not None:
                    _assert_reviewer_disjoint(dict(task), dict(reviewed))

            attempt = _attempt_for(task)
            result, jkey, succeeded = await _dispatch_one_attempt(task, attempt, budget)

            # M6b-1 — the NESTED REVIEW-FIX LOOP. An IMPLEMENT task that (a) reached
            # terminal SUCCESS and (b) has a paired reviewer drives a nested loop on
            # a CHILD BudgetPool(parent=budget) + the SAME shared semaphore: dispatch
            # the reviewer; on a BLOCK verdict re-dispatch the implement (attempts
            # incremented so `_attempt_for` advances + MAX_ATTEMPTS bounds it), then
            # re-review; until PASS or the ≤MAX_ATTEMPTS bound. On a reviewer ABANDON
            # (worker failure, NOT a BLOCK verdict) the implement is NOT re-dispatched
            # — the reviewer cascades blocked to its dependents (T5). The implement's
            # own terminal state is finalized in the `finally` from `result`/`succeeded`
            # as updated by the loop.
            if succeeded and tid in review_by_implement:
                result, jkey, succeeded = await _run_review_fix_loop(task, result, jkey)
        except BaseException as exc:
            # A non-BudgetExceeded raise (CloneEscapeError, an eager-merge CONFLICT
            # surfaced from `_dispatch_one_attempt`, the T6 disjointness raise, …):
            # record a fail-closed result so the finalize marks the task terminal +
            # notifies (the admission loop never hangs), then RE-RAISE so the
            # post-loop drain surfaces it. The T6 disjointness raise is the one we
            # WANT to surface loudly.
            if result is None:
                result = FAILED_ATTEMPT
            succeeded = False
            await _finalize_task(task, result, jkey, succeeded)
            raise exc
        else:
            await _finalize_task(task, result, jkey, succeeded)

    async def _run_review_fix_loop(
        implement_task: Mapping[str, Any],
        implement_result: Any,
        implement_jkey: str | None,
    ) -> tuple[Any, str | None, bool]:
        """Drive the nested review→fix loop for a SUCCEEDED implement task.

        Returns the implement's FINAL ``(result, jkey, succeeded)`` (the implement
        is the task this caller finalizes). The paired REVIEWER's terminal state is
        finalized HERE (it is never admitted independently), so when this returns,
        the reviewer is already terminal/recorded.

        Loop:
          * dispatch the reviewer (re-admit the EXISTING reviewer task) on the CHILD
            budget + the SAME semaphore → classify the verdict;
          * PASS (reviewer ``done``) → implement stays success, reviewer finalized
            success → return;
          * ABANDON (reviewer worker failure — FAILED_ATTEMPT / `failed` /
            `abandoned`) → reviewer finalized abandoned (cascades to ITS dependents);
            the implement is NOT re-dispatched and stays success → return;
          * BLOCK (reviewer `blocked` / `needs-input` verdict) → re-dispatch the
            implement with attempts INCREMENTED; if the re-dispatch FAILS the
            implement becomes the failure (reviewer not finalized — never ran this
            round) → return; if it succeeds, re-review. On exhausting MAX_ATTEMPTS
            the implement is abandoned (category capacity, escalated once via
            finalize) and the (last) reviewer is finalized with its blocking verdict.

        Shares ONE concurrency counter (the outer `semaphore`) — NEVER a second —
        and a CHILD BudgetPool(parent=budget) whose charges BUBBLE to the parent.
        """
        impl_tid = _task_id(implement_task)
        review_tid = review_by_implement[impl_tid]
        reviewer_task = task_index[review_tid]
        # T6 — DISPATCH-TIME reviewer-disjointness re-check (M6b-1). BEFORE driving
        # the paired reviewer, re-assert separation-of-duties via the SAME comparator
        # the planner used at synthesis (one comparator, no drift): a reviewer
        # re-assigned to the implementer's persona AFTER synthesis is caught here.
        # Fail-LOUD — the raise propagates out to `_run_one`'s except, which records
        # a fail-closed terminal for the IMPLEMENT (so the loop never hangs) and
        # re-raises so pipeline's post-loop drain surfaces it.
        _assert_reviewer_disjoint(dict(reviewer_task), dict(implement_task))
        # CHILD budget: total = a snapshot of the parent's remaining headroom (so the
        # child's own gate trips when the shared budget is exhausted) and parent=budget
        # so every child charge BUBBLES to the parent's reconciliation totals.
        child_budget = BudgetPool(
            total_tokens=max(1, budget.remaining()),
            headroom=1.0,
            parent=budget,
        )
        # Mutable working copies so re-dispatches can advance `attempts` without
        # mutating the shared task_index dicts.
        work_impl: dict[str, Any] = dict(implement_task)
        work_review: dict[str, Any] = dict(reviewer_task)
        result = implement_result
        jkey = implement_jkey
        succeeded = True
        # ANTI-STALE-REPLAY — the REAL guard (do NOT remove the key_tracker.record
        # at the loop top): the reviewer's journal key is busted each round NOT by
        # the attempt number (`ResultJournal.key` IGNORES `attempt` — it is sha256
        # over task_id‖persona‖phase‖model‖briefing‖upstream_digest ONLY; see
        # result_journal.py:39-41,138-141), but by the IMPLEMENT's recorded upstream
        # ENVELOPE HASH changing each round. The chain: each fix re-dispatch carries
        # the prior BLOCK feedback hash (extra_upstream_hashes) → a different
        # implement key → and `validate_envelope` FORCES the envelope's `attempt`
        # field == the dispatched attempt (pm_dispatch_envelope.py), so a
        # re-dispatched implement's envelope necessarily differs → its envelope hash
        # differs → after key_tracker.record(impl_tid, jkey) at the loop top, the
        # reviewer's `upstream_digest` changes → the reviewer's key is busted → it
        # genuinely RE-REVIEWS the fix instead of replaying the stale $0 verdict.
        #
        # `review_base_attempt`/`review_round` advance the reviewer's `attempts` ONLY
        # for the envelope `attempt` field + the `-p` prompt's "(attempt N)" line —
        # they do NOT affect the journal key (kept for observability/audit, not as a
        # replay guard). Base from the reviewer's own pre-set `attempts` (0 → first
        # review at 1).
        review_base_attempt = _attempt_for(reviewer_task)
        review_round = 0
        # ACCUMULATED reviewer-feedback hashes (each round's BLOCK envelope). Each
        # fix re-dispatch threads ALL prior feedback as extra upstream hashes so the
        # fix is a content-genuinely-different journal dispatch every round (the key
        # ignores `attempt`); accumulating — not just the latest — also keeps a fix
        # distinct even if two rounds' BLOCK envelopes were byte-identical.
        fix_feedback_hashes: list[str] = []
        while True:
            # Record the CURRENT (succeeded) implement's journal key in the tracker
            # so the reviewer's `upstream_envelope_hashes` resolves THIS round's
            # implement output — a re-dispatched (fixed) implement has a different
            # envelope hash, so the reviewer re-reviews the fix, not a stale input.
            if jkey is not None:
                async with progress:
                    key_tracker.record(impl_tid, jkey)
            # ── review the current (succeeded) implement output ──────────────
            # (attempt is for the envelope field/prompt only — NOT a key guard; the
            # busted-key guard is the implement upstream-hash change recorded above.)
            review_attempt = review_base_attempt + review_round
            work_review["attempts"] = review_attempt - 1
            rev_result, rev_jkey, rev_succeeded = await _dispatch_one_attempt(
                work_review, review_attempt, child_budget
            )
            review_round += 1
            verdict = _reviewer_verdict(rev_result, rev_succeeded)
            if verdict == "pass":
                # Reviewer PASS — finalize the reviewer as a clean success; the
                # implement stays success.
                await _finalize_task(work_review, rev_result, rev_jkey, True)
                return result, jkey, True
            if verdict == "abandon":
                # Reviewer WORKER FAILURE (not a verdict) — finalize the reviewer as
                # abandoned; it cascades to ITS dependents. The implement is NOT
                # re-dispatched and stays success.
                await _finalize_task(work_review, rev_result, rev_jkey, False)
                return result, jkey, True
            # verdict == "block": the reviewer found issues — re-dispatch the
            # implement to FIX, attempts INCREMENTED so `_attempt_for` advances and
            # MAX_ATTEMPTS bounds the loop. Record the BLOCK envelope's hash as the
            # fix's feedback input (content-chaining: the fix is "implement, given
            # this review feedback" → a genuinely different journal dispatch).
            if rev_jkey is not None:
                fb = journal.get_envelope_hash(rev_jkey)
                if fb is not None:
                    fix_feedback_hashes.append(fb)
            next_attempt = _attempt_for(work_impl) + 1
            if next_attempt > MAX_ATTEMPTS:
                # Bound reached — abandon the implement (category capacity) and
                # finalize the (last) reviewer with its blocking verdict so both
                # tasks are terminal. The implement's capacity abandon escalates
                # exactly once via _finalize_task.
                logging.getLogger(__name__).warning(
                    "task %s ABANDONED (capacity): review-fix loop exhausted "
                    "MAX_ATTEMPTS=%d without a PASS verdict from reviewer %s.",
                    impl_tid,
                    MAX_ATTEMPTS,
                    review_tid,
                )
                abandon = _abandoned_result(
                    impl_tid,
                    category=_CATEGORY_CAPACITY,
                    upstream_task_id=None,
                    last_status=f"review-fix loop exhausted MAX_ATTEMPTS={MAX_ATTEMPTS}",
                )
                await _finalize_task(work_review, rev_result, rev_jkey, rev_succeeded)
                return abandon, None, False
            # Pre-set `attempts` so `_attempt_for` advances on the re-dispatch.
            work_impl["attempts"] = next_attempt - 1
            result, jkey, succeeded = await _dispatch_one_attempt(
                work_impl,
                next_attempt,
                child_budget,
                extra_upstream_hashes=list(fix_feedback_hashes),
            )
            if not succeeded:
                # The fix attempt itself failed (no reviewable output) — the
                # implement becomes this failure. FINALIZE THE REVIEWER with its
                # ACTUAL BLOCK verdict (rev_result/rev_jkey, succeeded=False) — the
                # reviewer DID run this round and authored a real BLOCK; recording
                # its true verdict (mirroring the MAX_ATTEMPTS-exhaust branch above)
                # gives a higher-fidelity abandonment report than letting the cascade
                # gate later stamp it as a structured blocked-CASCADE. No
                # double-finalize: _finalize_task adds the reviewer to `terminal`, and
                # the cascade gate SKIPS any task already in `terminal` (:1485). The
                # implement stays un-finalized here ⇒ the caller's except/else marks
                # it terminal as the failure; the reviewer is now terminal too.
                await _finalize_task(work_review, rev_result, rev_jkey, False)
                return result, jkey, False
            # Fix succeeded → loop back to re-review.

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
    # Escalations queued by the cascade gate in the CURRENT pass — fired AFTER the
    # `progress` lock is released (the user callback must not run under it). HOISTED
    # above the `try` (and CLEARED, not rebound, at the top of each pass) so the
    # `except BaseException` handler can flush any still-pending cascade escalations
    # if a later step in the SAME pass raises (e.g. the defensive MAX_ATTEMPTS gate)
    # — a cascade-abandoned task that already committed terminal should not lose its
    # escalation just because a LATER in-pass task raised. Delivery is BEST-EFFORT,
    # AT-MOST-ONCE: it SURVIVES a later in-pass raise (the M5 E1 invariant), but a
    # SINK that itself raises is not retried — a sink raising twice can drop the
    # remaining tail (pop-before-call guarantees the prefix never re-fires).
    # *(review E1; E1-R2 exactly-once)*
    cascade_escalations: list[dict[str, Any]] = []
    try:
        while len(terminal) < len(ordered):
            cascade_escalations.clear()
            async with progress:
                # M5 (1) — CASCADE GATE (the crux). BEFORE the readiness check, a
                # not-yet-spawned task with an abandoned ancestor is NEVER admitted:
                # it can never get correct upstream output. Mark it terminal with a
                # STRUCTURED blocked result naming the upstream, add it to
                # spawned+terminal+pipeline_abandoned (so its OWN descendants
                # cascade — transitivity, parity with pm_dispatch.py:648), charge NO
                # attempt, and queue its guaranteed escalation. Iterated in the
                # deterministic order; a task cascaded this pass updates
                # pipeline_abandoned immediately so a sibling depending on it (rare
                # within one pass) also cascades.
                for task in ordered:
                    tid = _task_id(task)
                    if tid in spawned or tid in terminal:
                        continue
                    upstream = _first_abandoned_ancestor(task)
                    if upstream is not None:
                        result = _abandoned_result(
                            tid,
                            category=_CATEGORY_BLOCKED,
                            upstream_task_id=upstream,
                            last_status="cascade",
                        )
                        results[tid] = result
                        spawned.add(tid)
                        terminal.add(tid)
                        pipeline_abandoned.add(tid)  # transitive cascade source
                        cascade_escalations.append(
                            {
                                "kind": "escalation",
                                "task_id": tid,
                                "worker": task.get("assigned_to"),
                                "attempt": int(task.get("attempts") or 0),  # NO charge
                                "category": _CATEGORY_BLOCKED,
                                "last_status": "cascade",
                                "upstream_task_id": upstream,
                            }
                        )

                # Admit every currently-ready, not-yet-spawned task
                # (deterministic order).  Independence is evaluated against the
                # LIVE in-flight set, incrementally including peers admitted in
                # this same pass.
                admitted_this_pass: list[Mapping[str, Any]] = []
                for task in ordered:
                    tid = _task_id(task)
                    if tid in spawned:
                        continue
                    # M6b-1 — a PAIRED REVIEWER is NOT admitted independently: it is
                    # driven by its implement task's nested review-fix loop (else it
                    # would be dispatched twice — once here, once in the loop). We do
                    # NOT mark it `spawned` (which would also hide it from the cascade
                    # gate): the implement records the reviewer's terminal state when
                    # the loop runs it, AND if the implement is itself cascade-abandoned
                    # (never runs the loop) the cascade gate then marks this reviewer
                    # blocked on its abandoned `depends_on: [implement]` upstream. Its
                    # `depends_on: [implement]` already holds it back from the readiness
                    # gate while the implement is in-flight; this skip is the backstop
                    # for a degenerate (same-wave / no-depends_on) pairing.
                    if tid in implement_by_review:
                        continue
                    # `live` = currently in-flight + peers admitted this pass.
                    live = dict(in_flight)
                    for adm in admitted_this_pass:
                        live[_task_id(adm)] = adm
                    if _ready(task, live):
                        admitted_this_pass.append(task)
                for task in admitted_this_pass:
                    tid = _task_id(task)
                    # M5 (8) — DEFENSIVE MAX_ATTEMPTS gate. pipeline() dispatches
                    # each task ONCE at attempt 1 today, but guard the obligation so
                    # a future re-dispatch can never silently exceed the §5.2
                    # 5-attempt budget (the Path A invariant). A breach is a
                    # SCHEDULER bug — fail loud, never silently over-dispatch.
                    _attempt = _attempt_for(task)
                    if _attempt > MAX_ATTEMPTS:
                        raise RuntimeError(
                            f"pipeline obligation (c) breach: task {tid!r} would "
                            f"dispatch at attempt {_attempt} > MAX_ATTEMPTS="
                            f"{MAX_ATTEMPTS}; a task must never be dispatched beyond "
                            "the per-task attempt budget (scheduler bug)."
                        )
                    spawned.add(tid)
                    in_flight[tid] = task
                if admitted_this_pass:
                    for task in admitted_this_pass:
                        coro_task = asyncio.ensure_future(_run_one(task))
                        running.add(coro_task)
                        all_workers.append(coro_task)
                        coro_task.add_done_callback(running.discard)
                elif cascade_escalations:
                    # The cascade gate marked tasks terminal this pass without
                    # dispatching any worker — that IS progress (terminal count
                    # rose). Re-loop to re-evaluate (more tasks may now be ready, or
                    # the run may be complete). Do NOT wait — there is no completion
                    # to wait for and the loop condition has changed.
                    pass
                elif in_flight:
                    # Nothing newly admittable AND nothing cascaded this pass, but
                    # work is in flight — wait for a completion to change the
                    # picture. The decide-and-wait is atomic under `progress` (held
                    # continuously since the admission decision), so a worker's
                    # notify_all() cannot be lost between the decision and the wait.
                    await progress.wait()
                else:
                    # Deadlock guard: nothing ready, nothing in flight, nothing
                    # cascaded, not all terminal.  Only possible on a malformed DAG
                    # that validate_dag would have rejected; fail loud, never hang.
                    remaining = [t for t in ordered if _task_id(t) not in terminal]
                    raise RuntimeError(
                        "pipeline stalled: no ready task and none in flight, but "
                        f"{[_task_id(t) for t in remaining]} are not terminal — "
                        "malformed DAG (should have failed validate_dag)."
                    )
            # Fire the cascade escalations OUTSIDE the lock (the user callback must
            # not run while holding the admission lock). Only the cascade-gate +
            # admission branches reach here without having awaited; the
            # `progress.wait()` branch leaves `cascade_escalations` empty (no task
            # was cascaded this pass), so nothing fires spuriously after a wake.
            #
            # EXACTLY-ONCE (E1-R2): POP each escalation BEFORE calling _escalate so
            # that if _escalate RAISES on the Kth item, items 1..K-1 are already
            # removed and the `except BaseException` re-flush below fires ONLY the
            # not-yet-fired remainder (K..end) — never re-firing 1..K-1. (A
            # for-loop, or a trailing .clear(), would let the except handler
            # double-fire the already-fired prefix when escalate_fn raises.)
            while cascade_escalations:
                _escalate(cascade_escalations.pop(0))
    except BaseException:
        # The admission loop itself raised (e.g. the deadlock guard or the
        # defensive MAX_ATTEMPTS gate).  FIRST flush any cascade escalations still
        # PENDING from the raising pass — a task already committed terminal+abandoned
        # this pass should not lose its escalation just because a LATER in-pass step
        # raised before the normal post-block flush ran. Delivery is BEST-EFFORT,
        # AT-MOST-ONCE: it survives the in-pass raise, but a sink that itself raises
        # here aborts the drain (errors are suppressed below) and can drop the tail
        # (review E1; E1-R2 exactly-once).
        #
        # POP-before-call (E1-R2) so this drain is itself exactly-once even if an
        # _escalate here raises (the suppress would otherwise swallow it and the
        # already-fired items would be gone from the list anyway): each item is
        # removed before it fires, so the suppressed-and-aborted drain never
        # re-fires what already went out, and the normal-flush prefix that already
        # fired was already popped, so it is NOT in this list. Suppress secondary
        # errors so this never masks the original exception. THEN drain in-flight
        # workers (best-effort), restore a clean base + remove leftover worktrees,
        # and re-raise.
        with contextlib.suppress(BaseException):
            while cascade_escalations:
                _escalate(cascade_escalations.pop(0))
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


# ── M6a: the FIRST production caller of pipeline() (host/CLI transport) ───────
#
# Until M6a, NOTHING in production constructed `CliDispatchTools` or called
# `pipeline()` — only tests did. This is that production caller: it builds the
# CLI dispatch (the recommend-backed `build_cli_dispatch_for_project` factory),
# computes the DAG proof, and drives `pipeline()` end to end with the SAME
# `_default_est_for` the leaf uses (so the per-tier budget seeding and the
# per-task `--model` come from one tier source). It is reached ONLY from the
# `ATELIER_TRANSPORT=cli` branch (scripts/dispatch.py::dispatch_host_pipeline);
# the bridge default never touches this path.


async def run_host_pipeline_for_project(
    tasks: Sequence[Mapping[str, Any]],
    *,
    clone_dir: str | Path,
    budget: BudgetPool,
    journal: ResultJournal,
    existing_files: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    team_id: str = "host-team",
    team_lead_name: str = "team-lead",
    wave_id: str = "wave-1",
    model_for: Callable[[Mapping[str, Any], int], str] | None = None,
    briefing_for: Callable[[Mapping[str, Any], int], str] | None = None,
    phase_procedure_for: Callable[[Mapping[str, Any]], str] | None = None,
    worktree_factory: Callable[[str], _Worktree] | None = None,
    runner: Runner | None = None,
    max_workers: int = MAX_PARALLEL_WORKERS,
    wall_clock_s: float = WALL_CLOCK_S,
    permission_mode: str | None = None,
    disallowed_tools: Sequence[str] | None = None,
    allowed_tools: Sequence[str] | None = _DEFAULT_ALLOWED_TOOLS,
    sandbox_wrap: SandboxWrap | None = None,
    escalate_fn: Callable[[Mapping[str, Any]], None] | None = None,
    max_budget_usd: float | None = None,
    review_pairing: Mapping[str, str] | None = None,
    run_mode: Any = None,
) -> list[dict[str, Any]]:
    """Drive the deterministic host pipeline for a project (M6a production caller).

    This reuses the SAME M6a seam BUILDERS the
    :func:`scripts.cli_dispatch.build_cli_dispatch_for_project` factory wires
    (:func:`scripts.cli_dispatch._host_model_for` — recommend-backed ``model_for``;
    :func:`scripts.cli_dispatch._host_briefing_for` — in-memory-roster
    ``briefing_for``; :func:`scripts.cli_dispatch._default_est_for`) and passes the
    seam CALLABLES to the M5 :func:`pipeline` scheduler. It does NOT construct the
    factory's ``CliDispatchTools`` object: ``pipeline()`` consumes seam callables
    directly (it builds each ``run_attempt`` itself), so a ``CliDispatchTools``
    instance would be an unused leaf (and an unused owned event loop). The factory
    (T1) is the sibling ``CliDispatchTools`` constructor for the ``parallel()``
    façade / a future leaf-owning caller. This function is the host/CLI transport's
    single-await dispatch entry point — it replaced the removed legacy dispatch
    queue + its per-turn poll servicer.

    The model-tier seam is the SHARED bridge policy (override > env
    ``ATELIER_MODEL_TIER`` > difficulty > PHASE_TIER > DEFAULT, then ROLE_FLOOR
    opus floor) sourced per-task; the same ``est_for`` the leaf uses
    (:func:`scripts.cli_dispatch._default_est_for`) seeds ``pipeline``'s fleet
    width, so the chosen tier sets BOTH the ``--model`` argv AND the per-tier
    budget seeding from one source (a wrong tier would compound — hence the single
    policy).

    ``runner`` / ``sandbox_wrap`` / ``permission_mode`` / ``disallowed_tools``
    default to the leaf's secure defaults (real runner, identity wrap → the
    mandatory-sandbox gate refuses an unsandboxed real run unless the operator
    attests via ``ATELIER_CLI_ALLOW_UNSANDBOXED=1``; a caller wires
    ``native_sandbox_wrap(clone)`` for a confined real run). Tests inject a
    ``FakeCliRunner`` (exempt from the sandbox gate — no real process).

    **M6b-2 — R-MODE (``run_mode``).** The resolved per-run cost/quality posture
    (:class:`~scripts.run_mode.RunMode`). ``None`` ⇒ ``resolve_run_mode()`` auto-
    resolves the saved-profile default (env ``ATELIER_RUN_MODE`` → DEFAULT_PROFILE's
    mode; currently ``cost-effective`` → ``cost-lean``, a NON-neutral mode; NEVER
    blocks). The mode fans out to FOUR levers, ALL gated so that an EXPLICITLY-NEUTRAL
    run mode (``balanced`` / a lever-neutral mode) is a byte-for-byte no-op vs the
    pre-M6b-2 wiring. NOTE: ``run_mode=None`` is NOT a no-op by default — it auto-
    resolves to the saved ``cost-lean`` posture (down-biased tiers + a narrowed
    budget/fleet); only ``balanced`` leaves every lever untouched:

    * (a) ``run_mode.posture`` → ``_host_model_for(env, posture)`` (per-task tier
      bias; ``neutral`` is no-op). An explicit ``model_for`` override bypasses this.
    * (b) the BudgetPool — UPSTREAM SIZING. When the mode is NON-neutral, a NEW pool
      is constructed from the passed pool's total scaled by
      ``run_mode.budget_ceiling_factor`` with ``run_mode.budget_headroom``; the
      passed pool is used UNCHANGED for a neutral mode (the no-op — no rebuild, no
      lost charges). The entrypoint never owns budget construction for the neutral
      path; it only re-sizes when a mode explicitly asks for a different ceiling.
    * (c) ``run_mode.max_workers`` caps ``max_workers`` (it only ever NARROWS; the
      ``min`` keeps it from widening past the caller's cap). ``None`` ⇒ unchanged.
    * (d) ``run_mode.orchestrator_model`` is ADVISORY-ONLY — surfaced by the SKILL,
      NEVER written to settings.json (R-MODE is transient/per-run; the once-per-
      version settings-rec flow is the SOLE settings.json writer).

    R-MODE NEVER calls ``apply_profile`` / writes ``~/.claude/settings.json`` here.

    Returns the per-task validated envelope (or failed-attempt sentinel) in the
    deterministic ``(parallel_group, task_id)`` order :func:`pipeline` produces.
    """
    from scripts.cli_dispatch import (
        _default_est_for,
        _host_briefing_for,
        _host_model_for,
        identity_sandbox_wrap,
        real_cli_runner,
    )
    from scripts.dag import compute_dag_proof
    from scripts.run_mode import resolve_run_mode

    resolved_env: Mapping[str, str] = env if env is not None else os.environ
    task_list = list(tasks)

    # M6b-2 — resolve the R-MODE run posture. None ⇒ resolve_run_mode() auto-resolves
    # the saved-profile default (env ATELIER_RUN_MODE → DEFAULT_PROFILE's mode;
    # currently cost-effective → cost-lean, which is NON-neutral; never blocks).
    # resolve_run_mode is PURE — it reads PROFILES + env only and NEVER writes
    # settings.json (the orchestrator model is advisory; the once-per-version
    # settings-rec flow is the sole writer). Only an EXPLICITLY-NEUTRAL run mode
    # (balanced / a lever-neutral mode) keeps every lever a byte-for-byte no-op;
    # an un-threaded run_mode=None auto-resolves to cost-lean, which IS non-neutral.
    eff_run_mode = run_mode if run_mode is not None else resolve_run_mode(env=resolved_env)
    # (a) the per-task posture lever — neutral ⇒ None ⇒ recommend() is byte-identical.
    posture = None if eff_run_mode.is_neutral else eff_run_mode.posture
    # (b) the BudgetPool lever — UPSTREAM SIZING. Re-size ONLY for a non-neutral
    # mode (a neutral mode reuses the passed pool UNCHANGED — no rebuild, no lost
    # charges, so the neutral no-op path is byte-identical). A non-neutral mode
    # constructs a fresh pool from the passed pool's total scaled by the mode's
    # ceiling factor, with the mode's headroom.
    #
    # PRECONDITION: the passed `budget` MUST be a fresh, un-charged TOP-OF-RUN pool —
    # the non-neutral rebuild reads `budget.total_tokens` (the BASE, NOT remaining())
    # and starts a NEW pool, so any prior charges / parent linkage on the passed pool
    # are intentionally NOT carried over. Every production caller passes a fresh pool
    # at run start, so this is safe today; documenting it pins the contract.
    eff_budget = budget
    if not eff_run_mode.is_neutral:
        eff_budget = BudgetPool(
            total_tokens=eff_run_mode.budget_total_for(budget.total_tokens),
            headroom=eff_run_mode.budget_headroom,
        )
    # (c) the fleet-width lever — cap max_workers (NARROWS only; min keeps the
    # caller's cap as the ceiling). None ⇒ unchanged (neutral no-op).
    eff_max_workers = max_workers
    if eff_run_mode.max_workers is not None:
        eff_max_workers = min(max_workers, eff_run_mode.max_workers)

    # The DAG proof: fail-closed over the SAME existing-files set the planner gate
    # used. `compute_dag_proof` validates the DAG first and raises on an invalid
    # one (a proof is only defined for a valid DAG).
    dag_proof = compute_dag_proof([dict(t) for t in task_list], existing_files=existing_files or ())

    # M6b-1 — the review-fix pairing. When the caller did not supply one, DERIVE it
    # from the in-memory task list (lazy import, parity with the compute_dag_proof
    # import) IFF any task carries a `reviews` field — `reviews` lives ONLY in the
    # in-memory list (never persisted), so the host path is its sole source. A run
    # with no review tasks yields an empty pairing ⇒ pipeline() is byte-for-byte the
    # pre-M6b-1 single-dispatch behavior.
    eff_review_pairing: Mapping[str, str]
    if review_pairing is not None:
        eff_review_pairing = review_pairing
    elif any(t.get("reviews") for t in task_list):
        from scripts.planner import build_review_pairing

        eff_review_pairing = build_review_pairing([dict(t) for t in task_list])
    else:
        eff_review_pairing = {}

    # The shared model-tier + roster-briefing seams (the SAME builders the
    # `build_cli_dispatch_for_project` leaf factory wires). `pipeline()` consumes
    # the seam callables DIRECTLY (it constructs each `run_attempt` itself — it does
    # NOT take a CliDispatchTools), so we build the seams here and pass them through.
    # A caller-supplied `model_for` / `briefing_for` overrides the default seam.
    pick_model = model_for if model_for is not None else _host_model_for(resolved_env, posture)
    brief = (
        briefing_for
        if briefing_for is not None
        else _host_briefing_for(
            clone_dir=clone_dir,
            team_id=team_id,
            team_lead_name=team_lead_name,
            wave_id=wave_id,
            phase_procedure_for=phase_procedure_for,
        )
    )

    # Resolve the leaf's secure defaults at call time (so a None passes through to
    # the documented default rather than overriding it with a frozen value).
    eff_runner = runner if runner is not None else real_cli_runner
    eff_sandbox = sandbox_wrap if sandbox_wrap is not None else identity_sandbox_wrap

    pipeline_kwargs: dict[str, Any] = {
        # The R-MODE-sized pool (b) — eff_budget IS budget for a neutral mode.
        "budget": eff_budget,
        "journal": journal,
        "dag_proof": dag_proof,
        "model_for": pick_model,
        "briefing_for": brief,
        "clone_dir": clone_dir,
        "worktree_factory": worktree_factory,
        "runner": eff_runner,
        # The SAME est_for the leaf threads into run_attempt — so the chosen tier
        # sets BOTH the --model argv AND the per-tier fleet/budget seeding from one
        # source (a wrong tier compounds; one policy avoids the divergence).
        "est_for": _default_est_for,
        # The R-MODE-capped fan-out (c) — eff_max_workers IS max_workers for neutral.
        "max_workers": eff_max_workers,
        "wall_clock_s": wall_clock_s,
        "sandbox_wrap": eff_sandbox,
    }
    if permission_mode is not None:
        pipeline_kwargs["permission_mode"] = permission_mode
    if disallowed_tools is not None:
        pipeline_kwargs["disallowed_tools"] = disallowed_tools
    if allowed_tools is not None:
        pipeline_kwargs["allowed_tools"] = allowed_tools
    if escalate_fn is not None:
        pipeline_kwargs["escalate_fn"] = escalate_fn
    if max_budget_usd is not None:
        pipeline_kwargs["max_budget_usd"] = max_budget_usd
    # Thread the pairing ONLY when non-empty so a run without review tasks is a
    # byte-for-byte no-op (pipeline() defaults `review_pairing` to None, normalized
    # internally to {}, so leaving it unset keeps the single-dispatch behavior).
    if eff_review_pairing:
        pipeline_kwargs["review_pairing"] = eff_review_pairing
    results = await pipeline(task_list, **pipeline_kwargs)
    # Post-run COST REPORT (M8 rec #2) — the only post-run consumer of the meter.
    # eff_budget is the pool that received the bubbled per-task charges (in a
    # non-neutral mode the passed `budget` carries ZERO), so its root-level
    # usage_breakdown() is the whole-run four-channel total. Logger side-effect
    # only: the return stays the byte/shape-identical per-task envelope list.
    logging.getLogger(__name__).info(format_usage_report(eff_budget.usage_breakdown()))
    return results
