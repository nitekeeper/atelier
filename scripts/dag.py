"""DAG validation gates for the planner's task list (atelier#57).

The team-mode planner (atelier#58) emits a task list where every task
carries a `parallel_group` integer (the wave), an optional `depends_on`
list of upstream task ids, and optional `reads` / `writes` file-path
sets. PM's dispatcher reads the list ordered by
`(parallel_group ASC, created_at ASC)` and dispatches each wave
concurrently. The four gates in this module reject malformed task
lists BEFORE the planner returns, so PM's dispatcher never sees a
broken DAG.

Validation gates (per atelier#57 + design `docs/specs/2026-05-25-
atelier-team-mode-design.md` §5.4):

1. **Acyclic** — Kahn's algorithm on `depends_on`. Reject cycles with
   the cycle path in the error message.
2. **No within-wave file contention** — tasks in the same
   `parallel_group` MUST NOT touch the same file (their `writes` sets
   must be disjoint). Concurrent writes to the same path race.
3. **Reads satisfiable** — every `reads` reference resolves to either
   a pre-existing file (caller-supplied `existing_files` set) OR the
   `writes` of an earlier-wave task in the same list.
4. **No orphan deps** — every `depends_on` references a `task_id` that
   exists in the same list.

Each gate has its own `DagValidationError` subclass so callers can
distinguish failures. The top-level `validate_dag` raises on the FIRST
failure (gates run in the order above); future enhancements may collect
all failures into a single multi-error report if needed.

Task dict shape (loose — required fields are checked, optional fields
are tolerated as empty):

```
{
    "task_id": "t-1",            # required: unique within the list
    "parallel_group": 1,         # required: int >= 1
    "depends_on": ["t-0"],       # optional: list of task_ids (default [])
    "reads":  ["src/util.py"],   # optional: files this task reads
    "writes": ["src/foo.py"],    # optional: files this task writes
}
```

Tasks may carry additional fields (e.g. `assigned_persona`, `phase`);
this module ignores them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

# ── Error hierarchy ────────────────────────────────────────────────────────


class DagValidationError(ValueError):
    """Base class for all DAG validation failures.

    Inherits from `ValueError` so callers that catch the broader
    "invalid input" category still see these failures, but the
    dedicated class hierarchy lets the planner distinguish gates.
    """


class CycleError(DagValidationError):
    """The dependency graph contains a cycle."""


class FileContentionError(DagValidationError):
    """Two tasks in the same `parallel_group` write the same file."""


class UnsatisfiableReadsError(DagValidationError):
    """A task `reads` a file that is neither pre-existing nor written
    by an earlier-wave task in the list."""


class OrphanDepsError(DagValidationError):
    """A task `depends_on` a `task_id` that is not in the list."""


# ── Public API ─────────────────────────────────────────────────────────────


def validate_dag(tasks: list[dict], *, existing_files: Iterable[str] | None = None) -> None:
    """Raise `DagValidationError` (or a subclass) on the first gate failure.

    Gates run in this order: orphan-deps → acyclic → file-contention →
    reads-satisfiable. The orphan check fires first because cycle
    detection and reads-satisfiability both implicitly assume every
    referenced task_id exists — an orphan dep would otherwise produce
    a confusing secondary error.

    `existing_files` is the set of files that already exist at the
    repo root (pre-planner state) — caller-supplied because the
    planner knows its working directory and the DAG validator does
    not touch the filesystem. Defaults to an empty set when not
    provided (every read must then resolve to an earlier-wave write).
    """
    _check_orphan_deps(tasks)
    _check_acyclic(tasks)
    _check_file_contention(tasks)
    _check_reads_satisfiable(tasks, existing_files=frozenset(existing_files or ()))


# ── Gate implementations ───────────────────────────────────────────────────


def _check_orphan_deps(tasks: list[dict]) -> None:
    """Gate 4: every `depends_on` references a task_id in the list.

    Runs first because the other gates implicitly assume all referenced
    task_ids exist — surfacing the orphan reference first gives a
    cleaner error message than a downstream KeyError or
    misclassification as a cycle.
    """
    known_ids = {task["task_id"] for task in tasks}
    for task in tasks:
        for dep in task.get("depends_on") or []:
            if dep not in known_ids:
                raise OrphanDepsError(
                    f"task {task['task_id']!r} depends_on {dep!r} which is not "
                    f"in the task list (known task_ids: {sorted(known_ids)})"
                )


def _check_acyclic(tasks: list[dict]) -> None:
    """Gate 1: Kahn's algorithm rejects cycles.

    On failure, the error message names the remaining (cycle-locked)
    task_ids so the planner can re-synthesize. Reporting the cycle
    PATH (not just the set) is a nice-to-have; with the current
    error-shape it's sufficient to surface "these tasks form a cycle"
    and let the planner re-plan.
    """
    indegree: dict[str, int] = {task["task_id"]: 0 for task in tasks}
    adjacency: dict[str, list[str]] = {task["task_id"]: [] for task in tasks}
    for task in tasks:
        for dep in task.get("depends_on") or []:
            # Edge: dep → task (task depends on dep, so dep must run first).
            adjacency[dep].append(task["task_id"])
            indegree[task["task_id"]] += 1

    # Kahn's: repeatedly remove zero-indegree nodes.
    ready = [tid for tid, deg in indegree.items() if deg == 0]
    visited = 0
    while ready:
        tid = ready.pop()
        visited += 1
        for downstream in adjacency[tid]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                ready.append(downstream)
    if visited != len(tasks):
        cycle_locked = sorted(tid for tid, deg in indegree.items() if deg > 0)
        raise CycleError(
            f"dependency graph contains a cycle involving tasks: {cycle_locked}. "
            f"Kahn's algorithm visited {visited}/{len(tasks)} tasks before getting stuck."
        )


def _check_file_contention(tasks: list[dict]) -> None:
    """Gate 2: no two tasks in the same `parallel_group` write the same file.

    Within-wave file contention causes concurrent writes to race, so
    the planner MUST assign contending tasks to different waves. This
    gate catches both intentional violations (planner bug) and
    accidental ones (specialist auto-extension writing the same fixture
    file across two tasks in the same wave).
    """
    # Group writes by wave; per-wave check that no file appears twice.
    writes_by_wave: dict[int, dict[str, str]] = {}
    for task in tasks:
        wave = task.get("parallel_group")
        if wave is None:
            # parallel_group=None would be rejected by a separate caller-
            # side check (planner is required to set it per spec §5.4).
            # We tolerate it here so the orphan/acyclic gates can also
            # complete on a partially-filled task list.
            continue
        wave_writes = writes_by_wave.setdefault(int(wave), {})
        for path in task.get("writes") or []:
            owner = wave_writes.get(path)
            if owner is not None and owner != task["task_id"]:
                raise FileContentionError(
                    f"wave {wave}: tasks {owner!r} and {task['task_id']!r} "
                    f"both write file {path!r} — concurrent writes would race. "
                    f"Move one to a different parallel_group."
                )
            wave_writes[path] = task["task_id"]


def _check_reads_satisfiable(tasks: list[dict], *, existing_files: frozenset[str]) -> None:
    """Gate 3: every `reads` resolves to a pre-existing file OR an
    earlier-wave task's `writes`.

    "Earlier wave" is strictly `parallel_group < this task's wave` —
    same-wave reads are NOT satisfiable from same-wave writes because
    the writer and the reader run concurrently (the read may observe
    the file before the write completes).
    """
    # Bucket writes by wave so we can union "everything before wave N".
    writes_by_wave: dict[int, set[str]] = {}
    for task in tasks:
        wave = task.get("parallel_group")
        if wave is None:
            continue
        writes_by_wave.setdefault(int(wave), set()).update(task.get("writes") or [])

    sorted_waves = sorted(writes_by_wave)

    # `prior_writes[N]` = union of writes in every wave strictly < N.
    prior_writes: dict[int, set[str]] = {}
    accum: set[str] = set()
    for wave in sorted_waves:
        prior_writes[wave] = set(accum)
        accum.update(writes_by_wave[wave])

    for task in tasks:
        wave = task.get("parallel_group")
        if wave is None:
            continue
        wave_int = int(wave)
        available = existing_files | prior_writes.get(wave_int, set())
        for path in task.get("reads") or []:
            if path not in available:
                raise UnsatisfiableReadsError(
                    f"task {task['task_id']!r} (wave {wave_int}) reads {path!r}: "
                    f"file is not pre-existing and is not written by any "
                    f"earlier-wave task. Either add the file to "
                    f"`existing_files`, move the producing task to an earlier "
                    f"wave, or drop the read."
                )


# ── DAG proof (M2) ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DagProof:
    """Precomputed independence relation + direct reads-from map for a valid DAG.

    Produced by :func:`compute_dag_proof` after ``validate_dag`` has passed.
    Consumed by M4's ``pipeline()`` and M3's driver when constructing
    ``ResultJournal.key`` calls.

    All fields are immutable; ``DagProof`` is fully hashable.

    ``_independent_pairs`` stores ``frozenset({a_id, b_id})`` for every pair
    of task ids that are PIPELINE-INDEPENDENT: write-disjoint (no shared
    ``writes`` path) AND neither directly-nor-transitively depends on the
    other (no ``depends_on`` or reads-from path in either direction).

    ``_reads_from_items`` is a ``frozenset`` of ``(task_id,
    frozenset_of_upstream_ids)`` tuples encoding the direct reads-from map.
    Each entry gives the task ids whose ``writes`` directly satisfy that
    task's ``reads`` (strictly-earlier-wave writers only, per gate 4).  This
    is the DIRECT set the host driver passes to
    ``ResultJournal.key(upstream_envelope_hashes=...)``.  Transitivity is
    achieved by content-chaining in the journal (see ``result_journal.py``
    module docstring), NOT by pre-expanding a closure here.
    """

    _independent_pairs: frozenset[frozenset[str]] = field(repr=False)
    # Stored as frozenset of (task_id, frozenset_of_upstream_ids) for hashability.
    _reads_from_items: frozenset[tuple[str, frozenset[str]]] = field(repr=False)

    def independent(self, a_id: str, b_id: str) -> bool:
        """Return True iff tasks *a_id* and *b_id* are pipeline-independent.

        Two tasks are pipeline-independent when:
          * they share NO ``writes`` path (write-disjoint), AND
          * neither directly-nor-transitively depends on the other
            (no ``depends_on``/reads-from path between them in either
            direction).

        Absence of a proof of independence ⇒ NOT independent (fail-closed).
        Pairs where ``a_id == b_id`` are trivially NOT independent.
        """
        if a_id == b_id:
            return False
        return frozenset({a_id, b_id}) in self._independent_pairs

    def reads_from(self, task_id: str) -> frozenset[str]:
        """Return the frozenset of task ids whose ``writes`` directly satisfy
        *task_id*'s ``reads`` (strictly-earlier-wave writers only).

        This is the DIRECT reads-from set — NOT a pre-expanded transitive
        closure.  The host driver passes these ids' envelope hashes to
        ``ResultJournal.key(upstream_envelope_hashes=...)`` for content-chaining.

        Returns an empty frozenset when *task_id* has no reads-from upstreams
        (it reads only pre-existing files, or nothing at all) or when *task_id*
        is not in the DAG.
        """
        for tid, upstreams in self._reads_from_items:
            if tid == task_id:
                return upstreams
        return frozenset()


def compute_dag_proof(
    tasks: list[dict],
    *,
    existing_files: Iterable[str] | None = None,
) -> DagProof:
    """Compute the ``DagProof`` for a VALID task list.

    Calls ``validate_dag(tasks, existing_files=existing_files)`` first and
    propagates any :class:`DagValidationError` — a proof is only defined for
    a valid DAG; this function fails closed when given an invalid one.

    The proof is a READ over the same relations the four gates already compute;
    it is deliberately NOT a reimplementation of those relations so it cannot
    drift from ``validate_dag``.

    Parameters
    ----------
    tasks:
        The task list to compute a proof for.
    existing_files:
        Pre-existing files at the repo root (same semantics as ``validate_dag``).
        Pass the same value you passed to ``validate_dag`` so the reads-from
        relation is consistent.
    """
    ef = frozenset(existing_files or ())
    # Fail closed: raises DagValidationError (or subclass) on invalid DAGs.
    validate_dag(tasks, existing_files=ef)

    # ── Build index structures (mirrors what the gates already computed) ─────

    # Map task_id → writes set.
    writes_of: dict[str, frozenset[str]] = {
        t["task_id"]: frozenset(t.get("writes") or []) for t in tasks
    }

    # Map task_id → wave.
    wave_of: dict[str, int] = {t["task_id"]: int(t["parallel_group"]) for t in tasks}

    all_ids = list(writes_of)

    # ── Build DIRECT reads-from map ─────────────────────────────────────────
    # For each task t: the set of tasks whose writes directly satisfy t's reads.
    # A task w satisfies t's read of path p iff:
    #   - w writes p, AND
    #   - wave_of[w] < wave_of[t]  (strictly earlier wave — same semantics as gate 4)
    # This is the DIRECT set only (not transitive closure).

    reads_from_map: dict[str, frozenset[str]] = {}
    for t in tasks:
        t_id = t["task_id"]
        t_wave = wave_of[t_id]
        t_reads = frozenset(t.get("reads") or [])
        if not t_reads:
            reads_from_map[t_id] = frozenset()
            continue
        writers: set[str] = set()
        for w in tasks:
            w_id = w["task_id"]
            if w_id == t_id:
                continue
            if wave_of[w_id] >= t_wave:
                continue  # must be strictly earlier wave
            if writes_of[w_id] & t_reads:
                writers.add(w_id)
        reads_from_map[t_id] = frozenset(writers)

    # ── Transitive closure of the FULL dependency graph ──────────────────────
    # The "dep-path" for independence purposes includes BOTH explicit depends_on
    # edges AND the implicit reads-from edges (A writes x, B reads x at a later
    # wave → B depends on A's output).  The spec says: "neither directly-nor-
    # transitively depends on the other (no depends_on/reads-from path between
    # them)".  Using both edge types here ensures write/read relationships between
    # tasks that share no explicit depends_on are still excluded from the
    # independence relation.
    #
    # "downstream[a]" = tasks that directly depend on a (via either edge type).
    downstream: dict[str, list[str]] = {tid: [] for tid in all_ids}
    for t in tasks:
        t_id = t["task_id"]
        # Explicit depends_on edges.
        for dep in t.get("depends_on") or []:
            downstream[dep].append(t_id)
        # Implicit reads-from edges (A writes something B reads → B dep-on A).
        for writer_id in reads_from_map.get(t_id, frozenset()):
            if t_id not in downstream[writer_id]:
                downstream[writer_id].append(t_id)

    def _reachable_from(start: str) -> frozenset[str]:
        """BFS: all task ids reachable from *start* via downstream edges."""
        visited: set[str] = set()
        stack = list(downstream.get(start, []))
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            stack.extend(downstream.get(cur, []))
        return frozenset(visited)

    # reachable[a] = frozenset of ids that transitively depend on a.
    reachable: dict[str, frozenset[str]] = {tid: _reachable_from(tid) for tid in all_ids}

    # ── Compute the independence relation ────────────────────────────────────
    # A and B are pipeline-independent iff:
    #   1. write-disjoint: writes_of[a] ∩ writes_of[b] == ∅
    #   2. no dep-path in either direction (depends_on OR reads-from):
    #      b not in reachable[a]  (a is NOT an ancestor of b)
    #      a not in reachable[b]  (b is NOT an ancestor of a)
    # Fail-closed: any absence of these conditions → NOT independent.

    independent_pairs: set[frozenset[str]] = set()
    for i, a_id in enumerate(all_ids):
        for b_id in all_ids[i + 1 :]:
            # 1. Write-disjoint check.
            if writes_of[a_id] & writes_of[b_id]:
                continue  # shared write → NOT independent
            # 2. Dep-path check (both directions, full graph including reads-from).
            if b_id in reachable[a_id]:
                continue  # a is ancestor of b → NOT independent
            if a_id in reachable[b_id]:
                continue  # b is ancestor of a → NOT independent
            independent_pairs.add(frozenset({a_id, b_id}))

    # Encode reads_from_map as an immutable frozenset of tuples for hashability.
    reads_from_items: frozenset[tuple[str, frozenset[str]]] = frozenset(
        (tid, upstreams) for tid, upstreams in reads_from_map.items()
    )

    return DagProof(
        _independent_pairs=frozenset(independent_pairs),
        _reads_from_items=reads_from_items,
    )
