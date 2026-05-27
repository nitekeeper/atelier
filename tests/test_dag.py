"""Tests for `scripts/dag.py` — the planner's task-list validation gates (atelier#57)."""

from __future__ import annotations

import pytest

from scripts.dag import (
    CycleError,
    DagValidationError,
    FileContentionError,
    OrphanDepsError,
    UnsatisfiableReadsError,
    validate_dag,
)


def _task(
    task_id: str,
    wave: int,
    *,
    depends_on: list[str] | None = None,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
) -> dict:
    """Build a minimal task dict for tests. All fields beyond `task_id`
    and `parallel_group` are optional in the validator's contract."""
    return {
        "task_id": task_id,
        "parallel_group": wave,
        "depends_on": depends_on or [],
        "reads": reads or [],
        "writes": writes or [],
    }


# ── Happy paths ───────────────────────────────────────────────────────────


def test_validate_dag_accepts_empty_list():
    """An empty task list is trivially valid."""
    validate_dag([])


def test_validate_dag_accepts_minimal_single_task():
    """One task with no deps / reads / writes passes every gate."""
    validate_dag([_task("t1", 1)])


def test_validate_dag_accepts_clean_3_wave_dag():
    """A linear 3-wave DAG: wave 1 creates file → wave 2 reads + writes →
    wave 3 reads + writes. No cycles, no contention, all reads resolved."""
    tasks = [
        _task("t1", 1, writes=["src/a.py"]),
        _task("t2", 2, depends_on=["t1"], reads=["src/a.py"], writes=["src/b.py"]),
        _task("t3", 3, depends_on=["t2"], reads=["src/b.py"], writes=["src/c.py"]),
    ]
    validate_dag(tasks)


def test_validate_dag_accepts_disjoint_writes_in_same_wave():
    """Two tasks in the same wave writing DIFFERENT files is fine."""
    tasks = [
        _task("t1", 1, writes=["src/a.py"]),
        _task("t2", 1, writes=["src/b.py"]),
    ]
    validate_dag(tasks)


def test_validate_dag_accepts_pre_existing_file_reads():
    """A task can `read` a pre-existing file passed via `existing_files`
    without an earlier-wave task writing it."""
    tasks = [_task("t1", 1, reads=["src/util.py"], writes=["src/foo.py"])]
    validate_dag(tasks, existing_files={"src/util.py"})


# ── Gate 1: cycles ───────────────────────────────────────────────────────


def test_cycle_two_tasks_raises():
    """t1 → t2 → t1 is a 2-node cycle."""
    tasks = [
        _task("t1", 1, depends_on=["t2"]),
        _task("t2", 2, depends_on=["t1"]),
    ]
    with pytest.raises(CycleError) as exc_info:
        validate_dag(tasks)
    # Error names both cycle-locked tasks.
    assert "t1" in str(exc_info.value)
    assert "t2" in str(exc_info.value)


def test_cycle_self_loop_raises():
    """A task that depends on itself is a degenerate 1-node cycle."""
    tasks = [_task("t1", 1, depends_on=["t1"])]
    with pytest.raises(CycleError):
        validate_dag(tasks)


def test_cycle_three_tasks_raises():
    """t1 → t2 → t3 → t1 is a 3-node cycle."""
    tasks = [
        _task("t1", 1, depends_on=["t3"]),
        _task("t2", 2, depends_on=["t1"]),
        _task("t3", 3, depends_on=["t2"]),
    ]
    with pytest.raises(CycleError):
        validate_dag(tasks)


# ── Gate 2: within-wave file contention ───────────────────────────────────


def test_file_contention_same_wave_same_file_raises():
    """Two tasks in wave 1 both writing src/a.py race."""
    tasks = [
        _task("t1", 1, writes=["src/a.py"]),
        _task("t2", 1, writes=["src/a.py"]),
    ]
    with pytest.raises(FileContentionError) as exc_info:
        validate_dag(tasks)
    assert "src/a.py" in str(exc_info.value)
    assert "wave 1" in str(exc_info.value)


def test_file_contention_different_waves_same_file_is_ok():
    """The same file written by tasks in DIFFERENT waves is NOT
    contention — waves run sequentially."""
    tasks = [
        _task("t1", 1, writes=["src/a.py"]),
        _task("t2", 2, depends_on=["t1"], writes=["src/a.py"]),  # overwrite is fine
    ]
    validate_dag(tasks)


def test_file_contention_three_tasks_one_pair_contends():
    """Triple wave-1 tasks; only two contend on a file."""
    tasks = [
        _task("t1", 1, writes=["src/a.py", "src/c.py"]),
        _task("t2", 1, writes=["src/b.py"]),
        _task("t3", 1, writes=["src/c.py"]),  # contends with t1 on src/c.py
    ]
    with pytest.raises(FileContentionError):
        validate_dag(tasks)


# ── Gate 3: reads satisfiable ─────────────────────────────────────────────


def test_reads_unsatisfiable_no_writer_no_preexisting_raises():
    """t1 reads a file that no earlier-wave task writes and isn't pre-
    existing → UnsatisfiableReadsError."""
    tasks = [_task("t1", 1, reads=["src/ghost.py"])]
    with pytest.raises(UnsatisfiableReadsError) as exc_info:
        validate_dag(tasks)
    assert "src/ghost.py" in str(exc_info.value)


def test_reads_same_wave_writer_is_not_satisfiable():
    """Same-wave writes do NOT satisfy reads — the reader runs
    concurrently and might observe the file before the write completes.
    """
    tasks = [
        _task("t1", 1, writes=["src/a.py"]),
        _task("t2", 1, reads=["src/a.py"]),  # SAME wave — not satisfiable
    ]
    with pytest.raises(UnsatisfiableReadsError):
        validate_dag(tasks)


def test_reads_earlier_wave_writer_is_satisfiable():
    """An earlier-wave task's `writes` DOES satisfy a later-wave `reads`."""
    tasks = [
        _task("t1", 1, writes=["src/a.py"]),
        _task("t2", 2, reads=["src/a.py"]),
    ]
    validate_dag(tasks)


def test_reads_existing_files_set_satisfies():
    """Pre-existing files (caller-supplied) satisfy reads with no writer."""
    tasks = [_task("t1", 1, reads=["src/util.py", "tests/conftest.py"])]
    validate_dag(tasks, existing_files={"src/util.py", "tests/conftest.py"})


def test_reads_partial_preexisting_partial_writer():
    """Mix: some reads resolved by existing_files, others by earlier-wave writes."""
    tasks = [
        _task("t1", 1, writes=["src/new.py"]),
        _task("t2", 2, reads=["src/new.py", "src/preexisting.py"]),
    ]
    validate_dag(tasks, existing_files={"src/preexisting.py"})


# ── Gate 4: orphan deps ───────────────────────────────────────────────────


def test_orphan_dep_unknown_task_id_raises():
    """A `depends_on` reference to an unknown task_id → OrphanDepsError."""
    tasks = [
        _task("t1", 1),
        _task("t2", 2, depends_on=["t-doesnt-exist"]),
    ]
    with pytest.raises(OrphanDepsError) as exc_info:
        validate_dag(tasks)
    assert "t-doesnt-exist" in str(exc_info.value)


def test_orphan_dep_with_known_dep_still_fails_on_unknown():
    """Mixed list of known + unknown deps: still raises."""
    tasks = [
        _task("t1", 1),
        _task("t2", 2, depends_on=["t1", "t-ghost"]),
    ]
    with pytest.raises(OrphanDepsError):
        validate_dag(tasks)


def test_orphan_deps_fires_before_cycle_check():
    """Orphan check runs first — a list with BOTH an orphan dep and
    a cycle surfaces the orphan first (cleaner diagnostic; cycle
    detection on a malformed graph would produce a confusing error)."""
    tasks = [
        _task("t1", 1, depends_on=["t-ghost"]),  # orphan
        _task("t2", 2, depends_on=["t3"]),
        _task("t3", 3, depends_on=["t2"]),  # cycle with t2
    ]
    with pytest.raises(OrphanDepsError):
        validate_dag(tasks)


# ── Error-class hierarchy ────────────────────────────────────────────────


def test_all_specific_errors_subclass_dag_validation_error():
    """Callers that catch the base `DagValidationError` see every gate."""
    assert issubclass(CycleError, DagValidationError)
    assert issubclass(FileContentionError, DagValidationError)
    assert issubclass(UnsatisfiableReadsError, DagValidationError)
    assert issubclass(OrphanDepsError, DagValidationError)


def test_dag_validation_error_subclasses_value_error():
    """Base class is ValueError so generic 'bad input' handlers still fire."""
    assert issubclass(DagValidationError, ValueError)


# ── Tolerance for optional / missing fields ─────────────────────────────


def test_tasks_without_optional_fields_pass():
    """Tasks may omit `depends_on`, `reads`, `writes` entirely (not even
    present in the dict). The validator treats them as empty lists."""
    tasks = [
        {"task_id": "t1", "parallel_group": 1},
        {"task_id": "t2", "parallel_group": 2},
    ]
    validate_dag(tasks)


def test_validator_ignores_extra_fields():
    """Tasks may carry `assigned_persona`, `phase`, etc. — validator
    only reads the four contract fields."""
    tasks = [
        {
            "task_id": "t1",
            "parallel_group": 1,
            "assigned_persona": "atelier-pm-1",
            "phase": "tdd",
            "description": "write tests",
        }
    ]
    validate_dag(tasks)
