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
