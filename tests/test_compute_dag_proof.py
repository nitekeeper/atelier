"""Tests for ``scripts.dag.compute_dag_proof`` and ``DagProof`` (atelier M2).

Coverage required by spec:
  (a) Fuzzed agreement — every independent pair genuinely is write-disjoint
      with no dep-path; every write-conflict / dep-path pair is NOT independent.
  (b) Write-conflict pair is absent from the independence relation.
  (c) Reads-from pair (A writes x, B reads x) is NOT independent, and
      reads_from(B) contains A.
  (d) reads_from returns DIRECT writers only (3-chain A→B→C:
      reads_from(C) == {B}, not {A, B}).
  (e) compute_dag_proof on an invalid DAG fails closed (DagValidationError).

The fuzz generator produces random-but-valid acyclic DAGs and verifies the
proof is a faithful read of the gate logic, never drifting from validate_dag.
"""

from __future__ import annotations

import random

import pytest

from scripts.dag import (
    DagValidationError,
    compute_dag_proof,
    validate_dag,
)

# ── Helpers ───────────────────────────────────────────────────────────────


def _task(
    task_id: str,
    wave: int,
    *,
    depends_on: list[str] | None = None,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "parallel_group": wave,
        "depends_on": depends_on or [],
        "reads": reads or [],
        "writes": writes or [],
    }


def _full_dep_adjacency(tasks: list[dict]) -> dict[str, list[str]]:
    """Build the FULL dependency adjacency (forward edges producer → consumer).

    The contract's notion of "dependent" is ``depends_on`` UNION reads-from:
      * explicit ``depends_on`` edge: dep → task (dep must run first), and
      * implicit reads-from edge: writer → reader, where the writer writes a
        file the reader ``reads`` from a STRICTLY-EARLIER wave (gate-4 semantics).

    This is the oracle counterpart of the production code's reachability closure,
    which walks BOTH edge types.  An oracle that only walked ``depends_on`` would
    silently agree with a buggy ``compute_dag_proof`` that dropped implicit
    reads-from edges (the exact mutant this fuzz must catch).
    """
    adjacency: dict[str, list[str]] = {t["task_id"]: [] for t in tasks}
    wave_of = {t["task_id"]: int(t["parallel_group"]) for t in tasks}
    writes_of = {t["task_id"]: set(t.get("writes") or []) for t in tasks}

    for t in tasks:
        t_id = t["task_id"]
        # Explicit depends_on edges: dep → t.
        for dep in t.get("depends_on") or []:
            if dep in adjacency:
                adjacency[dep].append(t_id)
        # Implicit reads-from edges: writer → t, when writer (strictly earlier
        # wave) writes a file t reads.
        t_reads = set(t.get("reads") or [])
        if t_reads:
            for w in tasks:
                w_id = w["task_id"]
                if w_id == t_id:
                    continue
                if wave_of[w_id] >= wave_of[t_id]:
                    continue  # must be strictly earlier wave (gate-4)
                if (writes_of[w_id] & t_reads) and t_id not in adjacency[w_id]:
                    adjacency[w_id].append(t_id)
    return adjacency


def _dep_path_exists(tasks: list[dict], src: str, dst: str) -> bool:
    """Return True if there is a directed path src → ... → dst in the FULL
    dependency graph (``depends_on`` UNION implicit reads-from edges)."""
    adjacency = _full_dep_adjacency(tasks)
    # BFS from src
    visited: set[str] = set()
    queue = [src]
    while queue:
        cur = queue.pop(0)
        if cur == dst:
            return True
        if cur in visited:
            continue
        visited.add(cur)
        queue.extend(adjacency.get(cur, []))
    return False


def _write_disjoint(tasks: list[dict], a_id: str, b_id: str) -> bool:
    writes_a = set(next(t.get("writes") or [] for t in tasks if t["task_id"] == a_id))
    writes_b = set(next(t.get("writes") or [] for t in tasks if t["task_id"] == b_id))
    return not (writes_a & writes_b)


# ── Fuzz generator ────────────────────────────────────────────────────────


def _generate_valid_dag(seed: int, n_tasks: int = 9, n_waves: int = 4) -> list[dict]:
    """Generate a random valid DAG for fuzz testing.

    Strategy:
    - Assign each task to a random wave 1..n_waves.
    - Assign unique writes per task (no within-wave contention).
    - For each later-wave task, attach 0..K edges from strictly-earlier-wave
      tasks.  Each edge is one of two FLAVOURS, chosen at random:
        * ``"both"``     — an explicit ``depends_on`` AND a coincident ``reads``
          of the producer's ``writes`` (the original, easy case);
        * ``"reads_only"`` — a ``reads`` of the producer's ``writes`` with NO
          explicit ``depends_on`` (the IMPLICIT reads-from edge — the path the
          buggy mutant drops).
      Allowing MULTIPLE parents produces diamonds; multi-wave chains produce
      transitive-only pairs (A→B→C with no A→C edge).
    - Every read resolves to a STRICTLY-EARLIER-wave write, so ``validate_dag``
      passes (gate-4).  Writes are globally unique, so gate-2/3 pass.

    Returns a task list that passes ``validate_dag``.  The generator does NOT
    fall back to a degenerate DAG: a generation that fails validation is a bug
    and should surface, not be silently masked.
    """
    rng = random.Random(seed)
    tasks = []
    for i in range(n_tasks):
        wave = rng.randint(1, n_waves)
        tasks.append(_task(f"t{i}", wave))

    # Sort by wave for easier dep assignment.
    tasks.sort(key=lambda t: (t["parallel_group"], int(t["task_id"][1:])))

    # Assign unique writes per task (no contention: each task gets its own file).
    for i, t in enumerate(tasks):
        t["writes"] = [f"file_{i}.py"]

    by_wave: dict[int, list[dict]] = {}
    for t in tasks:
        by_wave.setdefault(t["parallel_group"], []).append(t)

    waves = sorted(by_wave)
    for w_idx, wave in enumerate(waves):
        if w_idx == 0:
            continue
        earlier_tasks = [x for ww in waves[:w_idx] for x in by_wave[ww]]
        if not earlier_tasks:
            continue
        for t in by_wave[wave]:
            # Choose how many earlier-wave parents this task gets (0..2) —
            # >=2 produces diamonds.
            n_parents = rng.randint(0, min(2, len(earlier_tasks)))
            parents = rng.sample(earlier_tasks, n_parents)
            for parent in parents:
                pfile = parent["writes"][0]
                flavour = rng.choice(["both", "reads_only"])
                # Always add the reads edge (resolved from a strictly-earlier
                # wave write → gate-4 satisfied).
                if pfile not in t["reads"]:
                    t["reads"].append(pfile)
                if flavour == "both" and parent["task_id"] not in t["depends_on"]:
                    t["depends_on"].append(parent["task_id"])
                # flavour == "reads_only": deliberately NO depends_on edge —
                # this exercises the implicit reads-from dependency path.

    # Validate before returning — a failure is a generator bug, surface it.
    validate_dag(tasks)
    return tasks


def _reads_without_depends_on_edges(tasks: list[dict]) -> int:
    """Count reads-from edges that have NO coincident explicit depends_on edge.

    For each task t and each file p it reads, find the strictly-earlier-wave
    writer(s) of p; an edge writer→t is "reads-without-depends_on" when t does
    NOT list ``writer`` in its ``depends_on``.  This is the implicit-edge case
    the fuzz must cover."""
    wave_of = {t["task_id"]: int(t["parallel_group"]) for t in tasks}
    writes_of = {t["task_id"]: set(t.get("writes") or []) for t in tasks}
    count = 0
    for t in tasks:
        t_id = t["task_id"]
        deps = set(t.get("depends_on") or [])
        for p in t.get("reads") or []:
            for w in tasks:
                w_id = w["task_id"]
                if w_id == t_id or wave_of[w_id] >= wave_of[t_id]:
                    continue
                if p in writes_of[w_id] and w_id not in deps:
                    count += 1
    return count


def _has_transitive_chain(tasks: list[dict]) -> bool:
    """True if the corpus contains a transitive-only pair A→B→C (A reaches C
    only via B — there is no direct A→C edge)."""
    adjacency = _full_dep_adjacency(tasks)
    for a in adjacency:
        for b in adjacency[a]:  # a → b direct
            for c in adjacency.get(b, []):  # b → c direct
                if c != a and c not in adjacency[a]:
                    return True  # a → b → c, no direct a → c
    return False


def _has_diamond_or_multiparent(tasks: list[dict]) -> bool:
    """True if any task has >=2 distinct direct dependency parents (the in-edge
    fan-in that produces diamonds / multi-parent joins)."""
    adjacency = _full_dep_adjacency(tasks)
    indegree: dict[str, int] = {t["task_id"]: 0 for t in tasks}
    for _src, dsts in adjacency.items():
        for dst in dsts:
            indegree[dst] += 1
    return any(deg >= 2 for deg in indegree.values())


# ── (a) Fuzzed agreement ──────────────────────────────────────────────────


_FUZZ_SEEDS = range(60)


@pytest.mark.parametrize("seed", _FUZZ_SEEDS)
def test_fuzz_proof_agrees_with_gate_logic(seed: int) -> None:
    """For each generated valid DAG, ``proof.independent(a, b)`` must equal
    ``write_disjoint(a, b) AND NOT oracle_dep_path(a, b)``, where the oracle
    walks the FULL dependency graph (``depends_on`` UNION implicit reads-from).

    Because the oracle now walks implicit reads-from edges (matching the
    contract), this fuzz genuinely cross-checks the implicit-edge code path:
    a ``compute_dag_proof`` that dropped implicit edges would mark a
    reads-without-``depends_on`` pair INDEPENDENT while the oracle marks it
    dependent → the assert fires.  (Mutation-verified: see
    ``test_corpus_is_not_degenerate`` docstring.)"""
    tasks = _generate_valid_dag(seed)
    proof = compute_dag_proof(tasks)
    ids = [t["task_id"] for t in tasks]
    writes_of = {t["task_id"]: set(t.get("writes") or []) for t in tasks}
    # Precompute the full adjacency once, then reachability per node.
    adjacency = _full_dep_adjacency(tasks)

    def _reaches(src: str, dst: str) -> bool:
        visited: set[str] = set()
        queue = list(adjacency.get(src, []))
        while queue:
            cur = queue.pop()
            if cur == dst:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            queue.extend(adjacency.get(cur, []))
        return False

    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1 :]:
            is_indep = proof.independent(a_id, b_id)
            write_conflict = bool(writes_of[a_id] & writes_of[b_id])
            dep_ab = _reaches(a_id, b_id)
            dep_ba = _reaches(b_id, a_id)
            expected = (not write_conflict) and (not dep_ab) and (not dep_ba)
            assert is_indep == expected, (
                f"seed={seed}: independent({a_id}, {b_id})={is_indep} but oracle "
                f"expected {expected} (write_conflict={write_conflict}, "
                f"dep_ab={dep_ab}, dep_ba={dep_ba})"
            )


def test_corpus_is_not_degenerate() -> None:
    """The fuzz is only load-bearing if its corpus actually contains the
    interesting cases.  This guard fails loudly if the generator degenerates so
    that the implicit-edge path stops being exercised.

    Asserts, across the full fuzz seed range, the corpus contains:
      * >=1 reads-without-``depends_on`` edge (the implicit reads-from path —
        the exact case the dropped-implicit-edge mutant gets wrong);
      * >=1 transitive-only chain A→B→C (no direct A→C);
      * >=1 diamond / multi-parent join.

    MUTATION-VERIFIED: with these cases present, patching ``compute_dag_proof``
    to drop implicit reads-from edges from its reachability closure makes
    ``test_fuzz_proof_agrees_with_gate_logic`` FAIL on >=1 seed (a
    reads-without-``depends_on`` pair is wrongly marked independent).  An empty
    corpus here would silently re-tautologise the fuzz."""
    total_implicit = 0
    total_transitive = 0
    total_diamond = 0
    for seed in _FUZZ_SEEDS:
        tasks = _generate_valid_dag(seed)
        total_implicit += _reads_without_depends_on_edges(tasks)
        if _has_transitive_chain(tasks):
            total_transitive += 1
        if _has_diamond_or_multiparent(tasks):
            total_diamond += 1

    assert total_implicit >= 1, (
        "fuzz corpus contains ZERO reads-without-depends_on edges — the "
        "implicit reads-from path is not exercised; the fuzz is a tautology."
    )
    assert total_transitive >= 1, (
        "fuzz corpus contains ZERO transitive-only A→B→C chains — transitive "
        "reachability is not exercised."
    )
    assert total_diamond >= 1, (
        "fuzz corpus contains ZERO diamond / multi-parent joins — fan-in "
        "structural variety is not exercised."
    )


# ── (b) Write-conflict pair absent from independence relation ─────────────


def test_write_conflict_pair_not_independent() -> None:
    """Two tasks sharing a writes path are NOT independent, even in different waves."""
    tasks = [
        _task("a", 1, writes=["shared.py"]),
        _task("b", 2, writes=["shared.py"]),  # same file, different wave
    ]
    # Note: different waves so no FileContentionError (gate 2 is within-wave only).
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    assert not proof.independent("a", "b")
    assert not proof.independent("b", "a")


def test_write_conflict_same_wave_not_independent() -> None:
    """Two tasks in the SAME wave writing different files ARE independent."""
    tasks = [
        _task("a", 1, writes=["x.py"]),
        _task("b", 1, writes=["y.py"]),
    ]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    assert proof.independent("a", "b")


# ── (c) reads-from pair: NOT independent, reads_from(B) contains A ────────


def test_reads_from_pair_not_independent() -> None:
    """A writes x; B reads x (and depends on A) → NOT independent.
    reads_from(B) must contain A."""
    tasks = [
        _task("A", 1, writes=["x.py"]),
        _task("B", 2, depends_on=["A"], reads=["x.py"], writes=["y.py"]),
    ]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    assert not proof.independent("A", "B")
    assert not proof.independent("B", "A")
    assert "A" in proof.reads_from("B")


def test_reads_from_via_wave_no_explicit_depends_on() -> None:
    """A writes x (wave 1); B reads x (wave 2) with no explicit depends_on.
    Gate-4 allows this (wave 1 < wave 2).  reads_from(B) must contain A;
    the pair is NOT independent because A writes a file B reads."""
    tasks = [
        _task("A", 1, writes=["x.py"]),
        _task("B", 2, reads=["x.py"], writes=["y.py"]),
    ]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    # A writes x.py which B reads → NOT independent (write/read edge)
    assert not proof.independent("A", "B")
    assert "A" in proof.reads_from("B")


# ── (d) reads_from is DIRECT-only (3-chain A→B→C) ────────────────────────


def test_reads_from_is_direct_only_3_chain() -> None:
    """3-chain: A(wave 1) writes a.py; B(wave 2) reads a.py, writes b.py;
    C(wave 3) reads b.py, writes c.py.

    reads_from(C) must be {B} — NOT {A, B}.
    reads_from(B) must be {A}.

    This is the load-bearing test for M1's journal correctness: the host
    driver must NOT pass a transitive closure to ResultJournal.key."""
    tasks = [
        _task("A", 1, writes=["a.py"]),
        _task("B", 2, depends_on=["A"], reads=["a.py"], writes=["b.py"]),
        _task("C", 3, depends_on=["B"], reads=["b.py"], writes=["c.py"]),
    ]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)

    assert proof.reads_from("A") == frozenset()  # A has no upstreams
    assert proof.reads_from("B") == frozenset({"A"})
    assert proof.reads_from("C") == frozenset({"B"})  # NOT {A, B}
    assert "A" not in proof.reads_from("C"), "reads_from must be DIRECT only"


def test_reads_from_multiple_direct_writers() -> None:
    """C reads both a.py (written by A) and b.py (written by B).
    Both A and B are direct reads-from upstreams of C."""
    tasks = [
        _task("A", 1, writes=["a.py"]),
        _task("B", 1, writes=["b.py"]),
        _task("C", 2, depends_on=["A", "B"], reads=["a.py", "b.py"], writes=["c.py"]),
    ]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    assert proof.reads_from("C") == frozenset({"A", "B"})


def test_reads_from_empty_when_no_reads() -> None:
    """A task with no reads has an empty reads_from set."""
    tasks = [_task("A", 1, writes=["x.py"])]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    assert proof.reads_from("A") == frozenset()


def test_reads_from_unknown_task_returns_empty() -> None:
    """reads_from on an unknown task_id returns an empty frozenset."""
    tasks = [_task("A", 1, writes=["x.py"])]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)
    assert proof.reads_from("no-such-task") == frozenset()


# ── (e) Invalid DAG fails closed ──────────────────────────────────────────


def test_compute_dag_proof_fails_closed_on_invalid_dag() -> None:
    """compute_dag_proof on a DAG with an orphan dep raises DagValidationError
    (specifically OrphanDepsError) — the proof is only defined for valid DAGs."""
    tasks = [
        _task("A", 1),
        _task("B", 2, depends_on=["A", "ghost"]),
    ]
    with pytest.raises(DagValidationError):
        compute_dag_proof(tasks)


def test_compute_dag_proof_fails_closed_on_cycle() -> None:
    """compute_dag_proof on a cyclic DAG raises DagValidationError."""
    from scripts.dag import CycleError

    tasks = [
        _task("A", 1, depends_on=["B"]),
        _task("B", 2, depends_on=["A"]),
    ]
    with pytest.raises(CycleError):
        compute_dag_proof(tasks)


def test_compute_dag_proof_fails_closed_on_file_contention() -> None:
    """compute_dag_proof on a task list with within-wave write contention raises."""
    from scripts.dag import FileContentionError

    tasks = [
        _task("A", 1, writes=["conflict.py"]),
        _task("B", 1, writes=["conflict.py"]),
    ]
    with pytest.raises(FileContentionError):
        compute_dag_proof(tasks)


def test_compute_dag_proof_fails_closed_on_unsatisfiable_reads() -> None:
    """compute_dag_proof on a task list with unsatisfiable reads raises."""
    from scripts.dag import UnsatisfiableReadsError

    tasks = [_task("A", 1, reads=["ghost.py"])]
    with pytest.raises(UnsatisfiableReadsError):
        compute_dag_proof(tasks)


# ── DagProof symmetry and self-comparison ─────────────────────────────────


def test_independent_is_symmetric() -> None:
    """independent(a, b) == independent(b, a)."""
    tasks = [
        _task("a", 1, writes=["x.py"]),
        _task("b", 1, writes=["y.py"]),
    ]
    proof = compute_dag_proof(tasks)
    assert proof.independent("a", "b") == proof.independent("b", "a")


def test_task_not_independent_with_itself() -> None:
    """A task is never independent with itself (by definition)."""
    tasks = [_task("a", 1, writes=["x.py"])]
    proof = compute_dag_proof(tasks)
    assert not proof.independent("a", "a")


def test_dependent_pair_not_independent() -> None:
    """A depends on B → NOT independent (dep-path)."""
    tasks = [
        _task("B", 1, writes=["b.py"]),
        _task("A", 2, depends_on=["B"], reads=["b.py"], writes=["a.py"]),
    ]
    proof = compute_dag_proof(tasks)
    assert not proof.independent("A", "B")
    assert not proof.independent("B", "A")


def test_truly_independent_pair() -> None:
    """Two tasks in the same wave with disjoint writes and no dep-path ARE independent."""
    tasks = [
        _task("A", 1, writes=["x.py"]),
        _task("B", 1, writes=["y.py"]),
    ]
    proof = compute_dag_proof(tasks)
    assert proof.independent("A", "B")
    assert proof.independent("B", "A")


def test_poc_3_task_dag_independence() -> None:
    """The canonical PoC DAG: T1(writes a.txt) ∥ T2(writes b.txt), both → T3.

    T1 and T2 are INDEPENDENT (write-disjoint, no dep-path).
    T3 is NOT independent of either T1 or T2 (dep-path in both cases)."""
    tasks = [
        _task("T1", 1, writes=["a.txt"]),
        _task("T2", 1, writes=["b.txt"]),
        _task(
            "T3",
            2,
            depends_on=["T1", "T2"],
            reads=["a.txt", "b.txt"],
            writes=["c.txt"],
        ),
    ]
    validate_dag(tasks)
    proof = compute_dag_proof(tasks)

    # T1 ∥ T2 are independent.
    assert proof.independent("T1", "T2")
    assert proof.independent("T2", "T1")

    # T3 depends on both → NOT independent.
    assert not proof.independent("T1", "T3")
    assert not proof.independent("T2", "T3")
    assert not proof.independent("T3", "T1")
    assert not proof.independent("T3", "T2")

    # reads_from(T3) = {T1, T2} directly.
    assert proof.reads_from("T3") == frozenset({"T1", "T2"})
    assert proof.reads_from("T1") == frozenset()
    assert proof.reads_from("T2") == frozenset()


def test_empty_task_list_produces_empty_proof() -> None:
    """An empty task list is valid; the proof has no pairs and no reads_from entries."""
    proof = compute_dag_proof([])
    assert proof.independent("x", "y") is False  # no tasks → fail closed


def test_dagproof_is_hashable() -> None:
    """DagProof is a frozen dataclass and must be hashable (for caching use)."""
    tasks = [_task("a", 1, writes=["x.py"])]
    proof = compute_dag_proof(tasks)
    # Should not raise.
    h = hash(proof)
    assert isinstance(h, int)
