"""Host plan phase — thin orchestration for the deterministic host engine (M2).

Chains the planner's plan phase into a single call suitable for the M4 host
driver, returning both the raw task list and the ``DagProof`` the barrier-free
``pipeline()`` will consume (M4) and that M3's driver uses when constructing
``ResultJournal.key`` calls.

This module has NO production caller until M4 — it is additive only, blast
radius ~zero.

Injection seam
--------------
``run_plan_phase`` accepts a ``synthesize_fn`` callable (the same seam that
``planner.run_planner`` already exposes as its ``synthesize`` parameter).  In
tests this is a recorded fake; in production M3/M4 it will be a closure over
the CLI-subprocess call.  This keeps the plan-phase logic unit-testable with
no live agent spawns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from scripts.dag import DagProof, compute_dag_proof
from scripts.planner import (
    check_reviewer_disjointness,
    parse_task_list,
    validate_tasks,
)


def run_plan_phase(
    synthesize_fn: Callable[..., str],
    *,
    existing_files: set[str] | None = None,
) -> tuple[list[dict[str, Any]], DagProof]:
    """Drive the plan phase and return ``(tasks, dag_proof)``.

    Chains:
      1. ``synthesize_fn(error=None)`` — calls the injected synthesis callable
         to obtain the raw task-list text.
      2. ``parse_task_list`` — parses the raw text into a list of task dicts
         (raises :class:`~scripts.planner.PlannerSynthesisFailure` on failure).
      3. ``validate_tasks`` — runs gate-0 (parallel_group), ``dag.validate_dag``
         (gates 1-4), and ``check_reviewer_disjointness`` on the parsed list
         (raises :class:`~scripts.planner.PlannerDagInvalid` on failure).
      4. ``check_reviewer_disjointness`` — already called inside ``validate_tasks``;
         re-exposed here as a named step for clarity and defence-in-depth.
      5. ``compute_dag_proof`` — computes the independence relation and direct
         reads-from map over the validated DAG.

    No retry logic is included here — ``run_plan_phase`` drives exactly ONE
    synthesis attempt.  The full retry/escalation loop with planner re-prompting
    lives in ``planner.run_planner`` and is available for callers that need it.
    The host driver (M4) can wrap this function with its own retry shell, or
    delegate to ``planner.run_planner`` directly.

    Parameters
    ----------
    synthesize_fn:
        Callable with signature ``synthesize_fn(error: str | None) -> str``.
        Called once with ``error=None``.  In tests, a recorded fake; in
        production, a closure over the CLI subprocess (M3).
    existing_files:
        Pre-existing files at the repo root, forwarded to ``validate_tasks``
        and ``compute_dag_proof`` for gate-4 / reads-from consistency.
        Defaults to an empty set when not supplied.

    Returns
    -------
    tuple[list[dict], DagProof]
        ``(tasks, dag_proof)`` — the validated task list and its precomputed
        proof.  Both are ready for the M4 scheduler.

    Raises
    ------
    PlannerSynthesisFailure
        When ``parse_task_list`` rejects the synthesis output.
    PlannerDagInvalid
        When ``validate_tasks`` or ``check_reviewer_disjointness`` rejects the
        task list.
    DagValidationError
        Propagated from ``compute_dag_proof`` if the task list somehow fails
        gate validation (should not happen after ``validate_tasks`` passes, but
        fail-closed).
    """
    ef = existing_files if existing_files is not None else set()
    raw = synthesize_fn(error=None)
    tasks = parse_task_list(raw)
    validate_tasks(tasks, existing_files=ef)
    # validate_tasks already calls check_reviewer_disjointness internally.
    # Explicit call here for defence-in-depth and spec-mandated chain visibility.
    check_reviewer_disjointness(tasks)
    dag_proof = compute_dag_proof(tasks, existing_files=ef)
    return tasks, dag_proof


__all__ = ["run_plan_phase"]
