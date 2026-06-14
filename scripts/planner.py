"""Plan-phase planner — sub-agent team mode (atelier#58).

Design refs: §5.4 (wave-based dispatch is mandatory; `parallel_group` not null),
§17 (sub-agent-mode plan phase = parallel specialist reads + planner synthesis),
§7.3/§17 failure semantics.

The plan phase runs in two orchestration waves (built by the PM's `Agent`
dispatches — this module CANNOT spawn agents):

  * **Wave 0 — parallel specialist reads.** The PM infers 3-7 specialist
    personas and dispatches them in parallel; each writes a field-analysis doc
    to the durable backend (``domain=research, subdomain=field-analysis``). See
    ``internal/plan-wave-0/SKILL.md``.
  * **Wave 1 — planner synthesis.** A single planner sub-agent consolidates the
    spec + field-analysis docs into a task list. See
    ``internal/plan-wave-1/SKILL.md``.

This module is the *deterministic backend* for wave 1: it parses the synthesis
agent's emitted task list, gates it, and persists it. The agent dispatch is
injected as the ``synthesize`` callable so the control flow is unit-testable
without spawning agents.

Failure semantics (§17 locked rule + #58 acceptance):

  * **SYNTHESIS-FAILURE** — the synthesis agent produced no parseable task list
    (empty / not JSON / not a non-empty list). There is nothing to correct, so
    escalate immediately with **no auto-retry** (§17: "synthesis-failure →
    one-shot escalate, no auto-retry").
  * **DAG-INVALID** — a well-formed list was produced but fails a deterministic
    gate (null ``parallel_group`` per §5.4, or ``dag.validate_dag``). This is a
    fixable defect, so the planner gets **exactly ONE re-prompt-to-fix retry**
    (the validator message is fed back to the synthesis agent), then escalates
    (#58: "planner re-synthesizes on a single DagValidationError before giving
    up"). The retry counter and escalation live HERE (deterministic), not in
    the SKILL markdown.

Reviewer-disjointness enforcement (§5.2/§19) is sibling **#59** — intentionally
NOT enforced in this module. The PM-side wave dispatch loop is sibling **#60**.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import tasks as tasks_mod
from scripts.dag import DagValidationError, validate_dag
from scripts.git_utils import git

# The synthesis prompt asks for ONE fenced ```json``` block; the LAST fenced
# block wins (a robust contract if the agent narrates before the payload).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


class PlannerError(RuntimeError):
    """Base for planner control-flow signals."""


class PlannerSynthesisFailure(PlannerError):
    """No parseable task list was produced — escalate immediately, 0 retry."""


class PlannerDagInvalid(PlannerError):
    """A task list was produced but fails a deterministic gate — 1 retry."""


class PlannerEscalation(PlannerError):
    """The planner gave up. Surfaced to the PM for one-shot human escalation.

    ``kind`` is ``"synthesis-failure"`` or ``"dag-invalid"``; ``detail`` is the
    underlying reason (the verbatim validator message for the dag-invalid
    path); ``attempts`` is how many synthesis attempts were consumed.
    """

    def __init__(self, kind: str, detail: str, *, attempts: int) -> None:
        self.kind = kind
        self.detail = detail
        self.attempts = attempts
        super().__init__(f"planner escalation ({kind}, after {attempts} attempt(s)): {detail}")


def snapshot_existing_files(root: str | Path) -> set[str]:
    """Return the set of git-tracked files at ``root``.

    Passed to ``validate_dag`` as ``existing_files`` so a task that ``reads`` a
    pre-existing repo file satisfies gate 3 (reads-satisfiable) without a
    producing task. Computed from the clone tree — NEVER from specialist docs
    (those are untrusted data). Tolerant: returns an empty set when ``root`` is
    not a git repo or git is unavailable, so a planner run is never aborted by
    a missing snapshot (gate 3 just becomes stricter)."""
    try:
        proc = git(["ls-files"], cwd=Path(root), check=False)
    except (OSError, ValueError):
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def parse_task_list(raw: str) -> list[dict[str, Any]]:
    """Parse the synthesis agent's emitted task list into a list of dicts.

    Accepts a bare JSON array or a fenced ```json``` block (LAST fence wins).
    Raises :class:`PlannerSynthesisFailure` on empty / unparseable / non-list /
    empty-list / non-object-entry output — there is no artifact to gate, so the
    caller escalates with no retry."""
    if not raw or not raw.strip():
        raise PlannerSynthesisFailure("synthesis produced empty output")
    fences = _FENCE_RE.findall(raw)
    candidate = fences[-1].strip() if fences else raw.strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as e:
        raise PlannerSynthesisFailure(f"synthesis output is not valid JSON: {e}") from e
    if not isinstance(parsed, list) or not parsed:
        raise PlannerSynthesisFailure(
            f"synthesis output is not a non-empty task list (got {type(parsed).__name__})"
        )
    if not all(isinstance(t, dict) for t in parsed):
        raise PlannerSynthesisFailure("synthesis task list contains a non-object entry")
    return parsed


def _require_parallel_group(tasks: list[dict[str, Any]]) -> None:
    """Gate 0 (§5.4): every task MUST carry an integer ``parallel_group`` >= 1.

    ``validate_dag`` TOLERATES ``parallel_group=None`` (its file-contention and
    reads gates ``continue`` past null-wave tasks), so a null wave would slip
    through dag validation and only blow up at PM dispatch. This is therefore
    the planner's OWN gate, run before ``validate_dag``."""
    for t in tasks:
        wave = t.get("parallel_group")
        tid = t.get("task_id", "?")
        if wave is None:
            raise PlannerDagInvalid(
                f"task {tid!r} has null parallel_group; every task must declare "
                f"an integer wave (>= 1) per design §5.4"
            )
        # bool is an int subclass — reject it explicitly.
        if isinstance(wave, bool) or not isinstance(wave, int) or wave < 1:
            raise PlannerDagInvalid(
                f"task {tid!r} parallel_group must be an int >= 1, got {wave!r}"
            )


def validate_tasks(tasks: list[dict[str, Any]], *, existing_files: set[str] | None = None) -> None:
    """Run the deterministic gates before persistence, cheapest/most-structural
    first so exactly one defect-class surfaces per retry:

      1. ``_require_parallel_group`` — the parallel_group own-gate (§5.4).
      2. ``dag.validate_dag`` — orphan-deps / acyclic / file-contention /
         reads-satisfiable (structural graph gates).
      3. ``check_reviewer_disjointness`` — the reviewer separation-of-duties
         policy gate (atelier#59).

    Any failure raises :class:`PlannerDagInvalid` carrying the validator message
    verbatim, so the single retry can feed the exact defect back to the
    synthesis agent."""
    _require_parallel_group(tasks)
    try:
        validate_dag(tasks, existing_files=existing_files)
    except DagValidationError as e:
        raise PlannerDagInvalid(f"{type(e).__name__}: {e}") from e
    # Policy gate runs AFTER the structural gates (cleaner errors first) and
    # before persist. It raises PlannerDagInvalid directly, so it rides the
    # existing run_planner single-retry-then-escalate path with no changes.
    check_reviewer_disjointness(tasks)


def check_reviewer_disjointness(tasks: list[dict[str, Any]]) -> None:
    """Reject a task list that violates reviewer disjointness (atelier#59).

    Separation-of-duties / no-self-review: a ``review`` task's
    ``assigned_persona`` MUST differ from the ``assigned_persona`` of the
    implement task it reviews — a persona cannot impartially grade its own work
    (the integrity guarantee behind A4/P2/F9).

    A review task declares ``reviews: "<task_id>"`` (a SINGLE string naming the
    implement task it reviews); non-review tasks omit the key (``None``).
    ``reviews`` is validation-time-only metadata, NOT persisted — like
    ``depends_on``/``reads``/``writes``. (Wave-ordering — a review running after
    the work it reviews — is ORTHOGONAL: the synthesis prompt declares
    ``depends_on: [reviewed]`` on review tasks and ``dag.validate_dag``'s
    existing acyclic/wave gates enforce it. This gate is persona-only.)

    Standalone, pure, side-effect-free, and ``.get()``-safe, so the PM
    orchestrator (atelier#60) can call it VERBATIM at dispatch time on the same
    in-memory task list (defense in depth: one canonical predicate guards both
    synthesis-time here and dispatch-time in #60, with no drift).

    FAIL-CLOSED. Raises :class:`PlannerDagInvalid` directly (NOT a
    ``DagValidationError`` subclass — so it rides ``run_planner``'s existing
    ``except PlannerDagInvalid`` single-retry-then-escalate path with zero
    changes), on the FIRST violation, with a precise + affirmative message
    carrying a defect-class prefix. Reference validity is resolved BEFORE the
    persona comparison so a dangling/absent pointer can never vacuously pass
    ("disjoint by absence" is the silent fail-open hazard). Sub-checks, in order:

      1. ``reviews`` shape — must be a non-empty ``str`` (or absent/``None`` =
         not a review task). A non-str / empty value is rejected.
      2. self-reference — ``reviews == own task_id`` is rejected.
      3. orphan — ``reviews`` must name an in-list ``task_id``.
      4. persona-disjointness — both the reviewer's and the reviewed task's
         ``assigned_persona`` must be present (a missing one is a DEFECT, not
         "disjoint"), and they must DIFFER (exact-string compare on the
         canonical roster ids; no normalization).

    Only a review task is compared against the SINGLE task its ``reviews``
    names — never all-pairs — so two implement tasks sharing a persona is legal.
    """
    by_id = {t.get("task_id"): t for t in tasks}
    for t in tasks:
        reviews = t.get("reviews")
        if reviews is None:
            continue  # not a review task
        tid = t.get("task_id", "?")
        if not isinstance(reviews, str) or not reviews:
            raise PlannerDagInvalid(
                f"reviewer-disjointness: task {tid!r} has a non-string/empty "
                f"`reviews` value {reviews!r}. `reviews` must be the task_id "
                f"(string) of the implement task this task reviews, or omitted "
                f"for non-review tasks."
            )
        if reviews == tid:
            raise PlannerDagInvalid(
                f"self-review: task {tid!r} `reviews` itself — a task cannot "
                f"review its own work. Point `reviews` at a different (implement) task."
            )
        if reviews not in by_id:
            known = sorted(k for k in by_id if k is not None)
            raise PlannerDagInvalid(
                f"orphan-reviews: task {tid!r} `reviews` {reviews!r} which is not "
                f"in the task list (known task_ids: {known}). Point `reviews` at "
                f"an in-list implement task."
            )
        # Persona-disjointness is the SHARED predicate (one comparator, no drift):
        # synthesis-time here and dispatch-time in the host scheduler both call
        # `assert_reviewer_disjoint`, so the EXACT-STRING compare can never diverge.
        assert_reviewer_disjoint(t, by_id[reviews])


def assert_reviewer_disjoint(
    reviewer: dict[str, Any],
    reviewed: dict[str, Any],
) -> None:
    """The CANONICAL reviewer/reviewed persona-disjointness comparator (atelier#59).

    Raises :class:`PlannerDagInvalid` iff *reviewer* and *reviewed* violate
    separation-of-duties: either side is missing ``assigned_persona`` (a missing
    one is a DEFECT, not "disjoint by absence" — the silent fail-open hazard), or
    the two ``assigned_persona`` values are EQUAL (EXACT-STRING compare on the
    canonical roster ids; no normalization).

    SINGLE SOURCE OF TRUTH. :func:`check_reviewer_disjointness` calls this at
    synthesis time, and the host scheduler (:func:`scripts.host_scheduler.pipeline`)
    calls the SAME predicate at dispatch time — so the comparator never drifts
    between the plan gate and the dispatch re-check (defense in depth: a reviewer
    re-assigned to the implementer's persona AFTER synthesis is still caught).

    Pure, side-effect-free, ``.get()``-safe.
    """
    reviewer_id = reviewer.get("task_id", "?")
    reviewed_id = reviewed.get("task_id", "?")
    reviewer_persona = reviewer.get("assigned_persona")
    reviewed_persona = reviewed.get("assigned_persona")
    if not reviewer_persona or not reviewed_persona:
        raise PlannerDagInvalid(
            f"reviewer-disjointness: review task {reviewer_id!r} or the task "
            f"{reviewed_id!r} it reviews is missing assigned_persona (reviewer="
            f"{reviewer_persona!r}, reviewed={reviewed_persona!r}). Both must "
            f"name a persona so reviewer independence can be verified."
        )
    if reviewer_persona == reviewed_persona:
        raise PlannerDagInvalid(
            f"reviewer-disjointness: review task {reviewer_id!r} is assigned persona "
            f"{reviewer_persona!r}, the SAME persona as the implement task "
            f"{reviewed_id!r} it reviews. A reviewer MUST be a DIFFERENT persona "
            f"than the implementer (separation of duties, A4/P2/F9). "
            f"Re-assign {reviewer_id!r} to another persona."
        )


def build_review_pairing(tasks: list[dict[str, Any]]) -> dict[str, str]:
    """Return the BIDIRECTIONAL implement↔review task-id pairing (atelier M6b-1).

    Walks the in-memory task list and, for each REVIEW task (one that carries a
    validated ``reviews: "<task_id>"`` field), records BOTH directions in one dict:

      * ``implement_task_id -> review_task_id`` (so the implement dispatch can find
        its paired reviewer to run the nested review-fix loop), and
      * ``review_task_id -> implement_task_id`` (so the dispatch-time disjointness
        re-check can resolve the task a reviewer reviews).

    Reuses the same ``by_id`` index pattern as :func:`check_reviewer_disjointness`
    so pairing and disjointness share ONE canonical resolution. ASSUMES the list
    already passed :func:`check_reviewer_disjointness` (valid, non-self, in-list
    ``reviews`` pointers); it is ``.get()``-safe and skips any malformed/dangling
    pointer rather than raising. ``reviews`` exists ONLY in the in-memory task list
    (it is never persisted — see :func:`persist_tasks`), so this is the host path's
    sole source for the pairing.

    Pure, side-effect-free. A list with no review tasks yields ``{}``.
    """
    by_id = {t.get("task_id"): t for t in tasks}
    pairing: dict[str, str] = {}
    for t in tasks:
        reviews = t.get("reviews")
        if not isinstance(reviews, str) or not reviews:
            continue  # not a review task (or a malformed pointer — skip, .get-safe)
        review_id = t.get("task_id")
        if not isinstance(review_id, str) or not review_id:
            continue
        if reviews == review_id or reviews not in by_id:
            continue  # self-reference / dangling — skip (assume already validated)
        pairing[reviews] = review_id  # implement -> review
        pairing[review_id] = reviews  # review -> implement
    return pairing


def persist_tasks(
    tasks: list[dict[str, Any]],
    *,
    db_path: str,
    project_id: int,
    created_by: str,
    workspace_id: int = 1,
    team_pk: str | None = None,
) -> list[int]:
    """Persist a VALIDATED task list via the backend facade.

    Routes through ``tasks.create_task`` (NOT ``backend_local``/``backend_memex``
    directly — A2), populating ``parallel_group`` (the dispatch primitive,
    atelier#34) and ``assigned_to`` (the planner-assigned persona). Returns the
    created DB row ids in input order.

    ``team_pk`` (atelier#90 / migration 010) is the run/cycle correlation id
    stamped on every persisted row so ``scripts/status.py`` can scope a
    snapshot per-cycle. NULL by default — a NULL leaves the rows
    project-scoped (the status fallback), so this is a SAFE no-op when the
    orchestrator does not thread a correlation id.

    ORDERING SUBTLETY: ``persist_tasks`` runs at ``plan:approved`` BEFORE
    ``build_wave_dispatcher_for_project``, so ``team_pk`` must be known before
    the planner persists. It already is — the orchestrator allocates the same
    correlation string for the bridge queue (it scopes the whole cycle's
    bridge_requests per ``scripts/atelier_entrypoint.py``) and passes that same
    string here; no dispatch-time backfill is required on the live flow.

    All-or-nothing: if any row fails mid-loop, the rows already created are
    deleted before the exception propagates, so a partial task list never
    reaches the PM dispatcher. Only ``parallel_group`` + ``team_pk`` are
    durable; the ``depends_on``/``reads``/``writes`` graph is validation-time
    metadata (the dispatcher orders by ``(parallel_group ASC, created_at ASC)``
    per §5.4 and never needs the edges persisted)."""
    created: list[int] = []
    try:
        for t in tasks:
            title = str(t.get("task_id") or t.get("description") or "task")[:200]
            row = tasks_mod.create_task(
                db_path,
                project_id=project_id,
                title=title,
                created_by=created_by,
                description=t.get("description") or "",
                assigned_to=t.get("assigned_persona"),
                workspace_id=workspace_id,
                parallel_group=t["parallel_group"],
                team_pk=team_pk,
            )
            created.append(row["id"])
    except Exception:
        for tid in created:
            # best-effort rollback — a failed delete must not mask the original error
            with contextlib.suppress(Exception):
                tasks_mod.delete_task(db_path, tid)
        raise
    return created


def run_planner(
    *,
    synthesize: Callable[..., str],
    db_path: str,
    project_id: int,
    created_by: str,
    root: str | Path | None = None,
    existing_files: set[str] | None = None,
    workspace_id: int = 1,
    team_pk: str | None = None,
    max_attempts: int = 2,
) -> list[int]:
    """Drive wave-1 synthesis → gate → persist with the §17/#58 retry policy.

    ``synthesize(error=None)`` is the injected synthesis-dispatch callable: it
    returns the raw task-list text the planner sub-agent emitted. On a
    DAG-INVALID retry it is re-invoked with ``error=<validator message>`` so the
    agent can correct the specific defect.

    Returns the persisted DB task ids on success. Raises
    :class:`PlannerEscalation` when the planner gives up:

      * ``synthesis-failure`` (unparseable / empty) → escalate immediately, 0
        retries (§17).
      * ``dag-invalid`` → exactly one re-prompt-to-fix retry, then escalate
        (#58).

    ``max_attempts`` is the total synthesis-attempt cap (default 2 = one initial
    + one DAG-invalid retry); a hard cap so a defective planner can never loop.

    ``team_pk`` (atelier#90 / migration 010) is the run/cycle correlation id
    forwarded to :func:`persist_tasks` so every persisted task is stamped with
    its cycle. It is known BEFORE the planner runs — the orchestrator allocates
    the same string for the bridge queue (it scopes the whole cycle's
    bridge_requests) and threads it here; the planner persists at
    ``plan:approved``, which is before ``build_wave_dispatcher_for_project``, so
    the stamp lands at creation with no dispatch-time backfill. NULL by default
    (single-cycle / non-team flows leave rows project-scoped — the SAFE status
    fallback).
    """
    if existing_files is None:
        existing_files = snapshot_existing_files(root) if root is not None else set()
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        raw = synthesize(error=last_error)
        try:
            task_list = parse_task_list(raw)
        except PlannerSynthesisFailure as e:
            # No artifact to correct — escalate immediately, no retry (§17).
            raise PlannerEscalation("synthesis-failure", str(e), attempts=attempt) from e
        try:
            validate_tasks(task_list, existing_files=existing_files)
        except PlannerDagInvalid as e:
            last_error = str(e)
            if attempt < max_attempts:
                continue  # one re-prompt-to-fix retry feeding back the error (#58)
            raise PlannerEscalation("dag-invalid", last_error, attempts=attempt) from e
        return persist_tasks(
            task_list,
            db_path=db_path,
            project_id=project_id,
            created_by=created_by,
            workspace_id=workspace_id,
            team_pk=team_pk,
        )
    # Defensive: the loop always returns or raises.
    raise PlannerEscalation(  # pragma: no cover
        "dag-invalid", last_error or "exhausted", attempts=max_attempts
    )
