"""Tests for ``scripts.host_plan.run_plan_phase`` (atelier M2).

Verifies the full plan phase driven by a recorded fake synthesize_fn:
  - validated tasks + correct dag_proof returned on the happy path;
  - reviewer-disjointness still enforced (raises PlannerDagInvalid);
  - synthesis failure propagates as PlannerSynthesisFailure;
  - dag_proof independence + reads_from correct for the canonical 3-task DAG.

The existing test_dag.py / test_planner.py tests are the equivalence proof
that compute_dag_proof and validate_tasks did NOT perturb the underlying gates.
"""

from __future__ import annotations

import json

import pytest

from scripts.dag import DagProof
from scripts.host_plan import run_plan_phase
from scripts.planner import PlannerDagInvalid, PlannerSynthesisFailure

# ── Helpers ───────────────────────────────────────────────────────────────


def _fenced(tasks: list[dict]) -> str:
    return "```json\n" + json.dumps(tasks) + "\n```"


def _make_synth(raw: str):
    """Return a callable that records its call and returns *raw*."""
    state = {"calls": 0, "errors": []}

    def synthesize(error=None):
        state["calls"] += 1
        state["errors"].append(error)
        return raw

    synthesize.state = state  # type: ignore[attr-defined]
    return synthesize


def _canonical_3_task_dag() -> list[dict]:
    """T1(wave 1, writes a.txt) ∥ T2(wave 1, writes b.txt) → T3(wave 2)."""
    return [
        {
            "task_id": "T1",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": [],
            "writes": ["a.txt"],
            "description": "build a",
        },
        {
            "task_id": "T2",
            "assigned_persona": "sdet-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": [],
            "writes": ["b.txt"],
            "description": "build b",
        },
        {
            "task_id": "T3",
            "assigned_persona": "software-architect-1",
            "parallel_group": 2,
            "depends_on": ["T1", "T2"],
            "reads": ["a.txt", "b.txt"],
            "writes": ["c.txt"],
            "description": "combine",
        },
    ]


def _impl_review_pair(reviewer_persona: str = "code-reviewer-1") -> list[dict]:
    return [
        {
            "task_id": "impl",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": [],
            "writes": ["foo.py"],
            "description": "implement",
        },
        {
            "task_id": "review",
            "assigned_persona": reviewer_persona,
            "parallel_group": 2,
            "depends_on": ["impl"],
            "reviews": "impl",
            "reads": ["foo.py"],
            "writes": [],
            "description": "review",
        },
    ]


# ── Happy path ────────────────────────────────────────────────────────────


def test_run_plan_phase_returns_tasks_and_proof() -> None:
    """Full plan phase with canonical DAG: returns (tasks, DagProof)."""
    dag = _canonical_3_task_dag()
    synth = _make_synth(_fenced(dag))
    tasks, _proof = run_plan_phase(synth)

    # Tasks are returned unchanged (in-memory; no DB persistence).
    assert len(tasks) == 3
    assert {t["task_id"] for t in tasks} == {"T1", "T2", "T3"}

    # synthesize was called exactly once with error=None.
    assert synth.state["calls"] == 1
    assert synth.state["errors"] == [None]


def test_run_plan_phase_returns_dag_proof_instance() -> None:
    """The second element of the return is a DagProof."""
    dag = _canonical_3_task_dag()
    synth = _make_synth(_fenced(dag))
    _, proof = run_plan_phase(synth)
    assert isinstance(proof, DagProof)


def test_run_plan_phase_proof_independence_correct() -> None:
    """T1 and T2 are independent; T3 is not independent of either."""
    dag = _canonical_3_task_dag()
    synth = _make_synth(_fenced(dag))
    _, proof = run_plan_phase(synth)

    assert proof.independent("T1", "T2")
    assert proof.independent("T2", "T1")
    assert not proof.independent("T1", "T3")
    assert not proof.independent("T2", "T3")
    assert not proof.independent("T3", "T1")
    assert not proof.independent("T3", "T2")


def test_run_plan_phase_proof_reads_from_correct() -> None:
    """reads_from(T3) == {T1, T2}; T1 and T2 have empty reads_from."""
    dag = _canonical_3_task_dag()
    synth = _make_synth(_fenced(dag))
    _, proof = run_plan_phase(synth)

    assert proof.reads_from("T3") == frozenset({"T1", "T2"})
    assert proof.reads_from("T1") == frozenset()
    assert proof.reads_from("T2") == frozenset()


def test_run_plan_phase_with_existing_files() -> None:
    """A task that reads a pre-existing file passes with existing_files supplied."""
    tasks = [
        {
            "task_id": "t1",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": 1,
            "depends_on": [],
            "reads": ["preexisting.py"],
            "writes": ["out.py"],
            "description": "use preexisting",
        }
    ]
    synth = _make_synth(_fenced(tasks))
    result_tasks, proof = run_plan_phase(synth, existing_files={"preexisting.py"})
    assert len(result_tasks) == 1
    assert proof.reads_from("t1") == frozenset()  # file-level, not task-level upstream


# ── Reviewer-disjointness enforcement ────────────────────────────────────


def test_run_plan_phase_enforces_reviewer_disjointness() -> None:
    """A same-persona reviewer raises PlannerDagInvalid (not silently skipped)."""
    bad = _impl_review_pair(reviewer_persona="backend-engineer-1")
    synth = _make_synth(_fenced(bad))
    with pytest.raises(PlannerDagInvalid, match="reviewer-disjointness"):
        run_plan_phase(synth)


def test_run_plan_phase_accepts_disjoint_reviewer() -> None:
    """A valid (disjoint) reviewer pair passes without error."""
    good = _impl_review_pair(reviewer_persona="code-reviewer-1")
    tasks, _proof = run_plan_phase(_make_synth(_fenced(good)))
    assert len(tasks) == 2


# ── Synthesis failure propagation ─────────────────────────────────────────


def test_run_plan_phase_propagates_synthesis_failure_empty() -> None:
    """Empty synthesis output raises PlannerSynthesisFailure."""
    synth = _make_synth("")
    with pytest.raises(PlannerSynthesisFailure):
        run_plan_phase(synth)


@pytest.mark.parametrize("raw", ["not json at all", "{}", "[]", "[1, 2, 3]"])
def test_run_plan_phase_propagates_synthesis_failure_bad_json(raw: str) -> None:
    """Unparseable or non-list JSON raises PlannerSynthesisFailure."""
    synth = _make_synth(raw)
    with pytest.raises(PlannerSynthesisFailure):
        run_plan_phase(synth)


# ── DAG validation propagation ────────────────────────────────────────────


def test_run_plan_phase_propagates_dag_invalid_orphan() -> None:
    """A task with an orphan dep raises PlannerDagInvalid."""
    tasks = [
        {
            "task_id": "t1",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": 1,
            "depends_on": ["ghost"],
            "reads": [],
            "writes": ["x.py"],
        }
    ]
    synth = _make_synth(_fenced(tasks))
    with pytest.raises(PlannerDagInvalid):
        run_plan_phase(synth)


def test_run_plan_phase_propagates_null_parallel_group() -> None:
    """A task with null parallel_group raises PlannerDagInvalid."""
    tasks = [
        {
            "task_id": "t1",
            "assigned_persona": "backend-engineer-1",
            "parallel_group": None,
            "depends_on": [],
            "reads": [],
            "writes": ["x.py"],
        }
    ]
    synth = _make_synth(_fenced(tasks))
    with pytest.raises(PlannerDagInvalid, match="parallel_group"):
        run_plan_phase(synth)


# ── synthesize_fn called with error=None ─────────────────────────────────


def test_run_plan_phase_calls_synthesize_with_error_none() -> None:
    """synthesize_fn must be called exactly once with error=None (no retry in run_plan_phase)."""
    dag = _canonical_3_task_dag()
    synth = _make_synth(_fenced(dag))
    run_plan_phase(synth)
    assert synth.state["calls"] == 1
    assert synth.state["errors"] == [None]
