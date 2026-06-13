"""Tests for cli_dispatch.direct_upstream_hashes — the M1 journal host-driver
contract helper (so M4 just calls it).

It maps a task's DIRECT reads-from upstream task ids (from a real DagProof) to
those upstreams' journal envelope hashes — the DIRECT set the ResultJournal key
consumes (content-chaining, NOT a pre-expanded transitive closure).
"""

from __future__ import annotations

from scripts.cli_dispatch import direct_upstream_hashes
from scripts.dag import compute_dag_proof
from scripts.result_journal import ResultJournal


def _tasks():
    # T1, T2 write-disjoint; T3 reads both → reads-from {T1, T2}.
    return [
        {"task_id": "t1", "parallel_group": 0, "writes": ["a.txt"]},
        {"task_id": "t2", "parallel_group": 0, "writes": ["b.txt"]},
        {
            "task_id": "t3",
            "parallel_group": 1,
            "depends_on": ["t1", "t2"],
            "reads": ["a.txt", "b.txt"],
            "writes": ["c.txt"],
        },
    ]


def test_direct_upstream_hashes_maps_reads_from_to_envelope_hashes():
    proof = compute_dag_proof(_tasks())
    journal = ResultJournal()

    # Journal each upstream under its task id (M3 self-contained resolution).
    env1 = {
        "type": "task_result",
        "task_id": "t1",
        "attempt": 1,
        "status": "done",
        "artifacts": [{"path": "a.txt", "sha": "1"}],
        "notes_md": "n",
    }
    env2 = {
        "type": "task_result",
        "task_id": "t2",
        "attempt": 1,
        "status": "done",
        "artifacts": [{"path": "b.txt", "sha": "2"}],
        "notes_md": "n",
    }
    journal.put("t1", env1, usage={"output_tokens": 1})
    journal.put("t2", env2, usage={"output_tokens": 1})

    hashes = direct_upstream_hashes("t3", proof, journal)
    # T3 reads-from {t1, t2} → exactly those two envelope hashes.
    assert hashes == frozenset({journal.get_envelope_hash("t1"), journal.get_envelope_hash("t2")})


def test_changed_upstream_envelope_changes_the_hash_set():
    """A different upstream envelope → a different hash → cache invalidation
    propagates (content-chaining)."""
    proof = compute_dag_proof(_tasks())
    j1 = ResultJournal()
    j2 = ResultJournal()
    base = {
        "type": "task_result",
        "task_id": "t1",
        "attempt": 1,
        "status": "done",
        "artifacts": [{"path": "a.txt", "sha": "1"}],
        "notes_md": "n",
    }
    changed = dict(base, notes_md="DIFFERENT")
    other = {
        "type": "task_result",
        "task_id": "t2",
        "attempt": 1,
        "status": "done",
        "artifacts": [{"path": "b.txt", "sha": "2"}],
        "notes_md": "n",
    }
    j1.put("t1", base, usage={"output_tokens": 1})
    j1.put("t2", other, usage={"output_tokens": 1})
    j2.put("t1", changed, usage={"output_tokens": 1})
    j2.put("t2", other, usage={"output_tokens": 1})

    assert direct_upstream_hashes("t3", proof, j1) != direct_upstream_hashes("t3", proof, j2)


def test_missing_upstream_contributes_no_hash():
    """An upstream not yet journaled (incomplete) contributes NO hash — the task
    is simply not yet replayable (the journal key misses)."""
    proof = compute_dag_proof(_tasks())
    journal = ResultJournal()
    # Only t1 journaled; t2 still in flight.
    journal.put(
        "t1",
        {
            "type": "task_result",
            "task_id": "t1",
            "attempt": 1,
            "status": "done",
            "artifacts": [{"path": "a", "sha": "s"}],
            "notes_md": "n",
        },
        usage={"output_tokens": 1},
    )
    hashes = direct_upstream_hashes("t3", proof, journal)
    assert hashes == frozenset({journal.get_envelope_hash("t1")})


def test_no_reads_from_returns_empty():
    """A task with no reads (t1) has an empty direct-upstream hash set."""
    proof = compute_dag_proof(_tasks())
    journal = ResultJournal()
    assert direct_upstream_hashes("t1", proof, journal) == frozenset()
