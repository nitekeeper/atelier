# tests/test_bridge_concurrency.py
"""Concurrency stress tests + invariant oracles for the team-mode bridge log.

Owner: ai-research-scientist-1 (Phase 4 wave 4, action item ``concurrency-harness``).
Spec: ``docs/specs/2026-05-25-atelier-team-mode-design.md`` (commit de1de0b04f)
plus the Phase 3 mesh close recorded on epic #37.

Per-sender FIFO + per-recipient gap-free seq CLAIMED;
cross-sender linearizability NOT claimed.

References
----------
* Kleppmann, *Designing Data-Intensive Applications*, ch. 9 (Consistency
  and Consensus). We borrow the vocabulary — history, anomaly classes,
  per-key vs cross-key ordering — to bound what this suite proves.
* The bridge is a single-file SQLite log on a Python stack, so we
  deliberately do NOT pull in Jepsen / Elle: the
  ``N ∈ {2, 8, 32}`` writers x ``{threads, multiproc}`` x 200-msg matrix
  below is enough to exercise the ``BEGIN IMMEDIATE`` seq allocator,
  WAL writer-serialization, and the per-team ``UNIQUE(idempotency_key)``
  contract without leaving the standard library.

Consistency claims (CLAIMED here, structurally asserted below)
---------------------------------------------------------------
1. **Per-recipient gap-free monotonic seq** — for every recipient, the
   set of allocated seqs is exactly ``range(1, total + 1)``. No gap.
   No duplicate. Falsifies any seq-allocator race under
   ``BEGIN IMMEDIATE``.
2. **Per-sender FIFO** — the subsequence of seqs returned to a single
   sender (in send-call order) is strictly increasing. Two sends from
   the same writer can never reorder on the wire.
3. **Cursor replay correctness** — reading from cursor=k returns exactly
   ``total - k`` rows; reader cursor advances monotonically across
   sessions / crashes.
4. **Idempotent dedupe is identity-preserving** — replaying the same
   ``idempotency_key`` returns the *original* ``(seq,
   persona_snapshot_id)`` tuple — equality, not merely non-error.
   Prevents silent persona re-stamping on retry.

Consistency claims explicitly NOT made
--------------------------------------
* **Cross-sender linearizability.** Two writers A, B aiming at the same
  recipient may interleave at the SQL layer; we only guarantee
  per-sender FIFO + per-recipient gap-free seq. The team-mode-rules
  SKILL.md disclaims this in matching language so future reviewers do
  not over-read the test suite.

Chaos hook
----------
``test_sigkill_writer_mid_tx_no_torn_seq`` SIGKILLs a writer in a
``multiprocessing.Process`` while it is hammering the allocator. We
assert (a) the resulting seq set is still contiguous from 1, (b) no
"phantom" / partially-written row is observable, (c) a fresh
``read_once`` succeeds (i.e. no SQLite lock was leaked by the killed
child). This is the cheapest validation of WAL +
``synchronous=NORMAL`` durability we can run in CI.

Bench artifact
--------------
Per ``(N, scheduler)`` we record p50/p99 write latency to
``tests/_bridge_bench.json`` (gitignored). **No wall-clock assertions**
— CI timing is too noisy to fail a test on. The artifact is for trend
monitoring only.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import random
import signal
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.bridge_read import read_once
from scripts.bridge_send import send
from scripts.migrate import apply_migrations

REPO_ROOT = Path(__file__).parent.parent
MIGRATIONS_SHARED = REPO_ROOT / "migrations" / "shared"
BENCH_PATH = Path(__file__).parent / "_bridge_bench.json"

# Phase-3 mesh-close stress envelope. Bumping these breaks the contract;
# do it deliberately + update the SKILL.md disclaimer in lockstep.
MSGS_PER_WRITER = 200
N_WRITERS_MATRIX = [2, 8, 32]
SCHEDULERS = ["thread", "multiproc"]

# Seeded RNG for any nondeterministic choice (sender→writer assignment,
# payload jitter). Per task spec: random.Random(42).
RNG_SEED = 42

# Aggregated latency samples written to BENCH_PATH at session teardown.
# Keyed by f"{n_writers}-{scheduler}" → list[float] in seconds.
_BENCH: dict[str, list[float]] = {}


# ── Fixtures ───────────────────────────────────────────────────────────────


def _seed_team(db_path: str, *, n_senders: int) -> None:
    """Seed one team (T1) with n_senders + one recipient (team-lead).

    Tests bypass HMAC token verification by calling ``send()`` directly
    with an explicit ``sender_id``. The DB-layer composite sender FK
    on ``bridge_messages`` still requires every sender_id to exist in
    ``team_members``, so we provision the full roster up front.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO persona_snapshots (persona_version, persona_blob) VALUES (?, ?)",
            ("v1", "{}"),
        )
        conn.execute(
            "INSERT INTO teams (team_id, project_id, lead_role, status) VALUES (?, ?, ?, ?)",
            ("T1", "P1", "team-lead", "active"),
        )
        conn.execute(
            "INSERT INTO team_members (team_id, role_id, member_name, "
            "persona_snapshot_id) VALUES (?, ?, ?, ?)",
            ("T1", "team-lead", "team-lead", 1),
        )
        for i in range(n_senders):
            role = f"sender-{i:02d}"
            conn.execute(
                "INSERT INTO team_members (team_id, role_id, member_name, "
                "persona_snapshot_id) VALUES (?, ?, ?, ?)",
                ("T1", role, role, 1),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def team_db(tmp_path: Path) -> str:
    """Fresh DB with 32-sender roster seeded (covers the largest N)."""
    db = tmp_path / "atelier.db"
    apply_migrations(str(db), MIGRATIONS_SHARED)
    _seed_team(str(db), n_senders=max(N_WRITERS_MATRIX))
    return str(db)


@pytest.fixture(scope="session", autouse=True)
def _flush_bench_artifact():
    """Write p50/p99 per (N, scheduler) to BENCH_PATH at session end.

    Module-local + autouse so the artifact is updated whenever this file
    runs, with no opt-in required from CI. The file is gitignored so
    benchmark noise never lands in commits.
    """
    yield
    if not _BENCH:
        return
    out: dict[str, dict[str, float | int]] = {}
    for key in sorted(_BENCH.keys()):
        samples = sorted(_BENCH[key])
        n = len(samples)
        if n == 0:
            continue
        p50 = statistics.median(samples)
        # Index-floor p99; clamp to last element for tiny samples.
        p99 = samples[min(n - 1, int(n * 0.99))]
        out[key] = {
            "n_samples": n,
            "p50_ms": round(p50 * 1000.0, 3),
            "p99_ms": round(p99 * 1000.0, 3),
        }
    out["_meta"] = {
        "msgs_per_writer": MSGS_PER_WRITER,
        "matrix_n_writers": N_WRITERS_MATRIX,
        "schedulers": SCHEDULERS,
        "rng_seed": RNG_SEED,
        "note": "Latency artifact only. No wall-clock assertions (CI flake).",
    }
    BENCH_PATH.write_text(json.dumps(out, indent=2, sort_keys=True))


# ── Top-level writer helpers (must be picklable for multiprocessing) ──────


def _writer_burst(args: tuple) -> tuple[str, list[int], list[float]]:
    """Send ``n_msgs`` messages as ``sender_id`` → ``recipient``.

    Returns ``(sender_id, seqs_in_send_order, latencies_seconds)`` so
    the caller can assert per-sender FIFO and aggregate latencies. Each
    call to ``send()`` opens its own SQLite connection (per
    ``scripts.bridge_send._open_db``), so threads and processes share
    no handle state — exactly the production access pattern.
    """
    db_path, team_id, recipient, sender_id, n_msgs = args
    seqs: list[int] = []
    lats: list[float] = []
    for i in range(n_msgs):
        t0 = time.perf_counter()
        r = send(
            db_path,
            team_id=team_id,
            recipient=recipient,
            sender_id=sender_id,
            kind="reply",
            payload=f"{sender_id}-{i}",
        )
        lats.append(time.perf_counter() - t0)
        seqs.append(int(r["seq"]))
    return sender_id, seqs, lats


def _chaos_writer_loop(db_path: str, team_id: str, recipient: str, sender_id: str) -> None:
    """Top-level loop body for the SIGKILL chaos test.

    Sends until killed. Wrapping each call in a try/except swallows the
    last-write-on-kill error if SIGKILL lands inside ``send()``; in
    practice SIGKILL is unblockable so the process simply vanishes
    mid-call — but the broad except keeps us safe if the OS scheduler
    ever delivers it on a Python instruction boundary.
    """
    i = 0
    while True:
        with contextlib.suppress(Exception):
            send(
                db_path,
                team_id=team_id,
                recipient=recipient,
                sender_id=sender_id,
                kind="reply",
                payload=f"chaos-{i}",
            )
        i += 1


# ── Invariant oracles ─────────────────────────────────────────────────────


def _assert_no_gap_no_dup(db_path: str, team_id: str, recipient: str, expected_total: int) -> None:
    """Oracle (1): seq_set == range(1, total+1) per recipient.

    Reads the raw bridge log (bypassing read_once's UNTRUSTED fence) so
    the assertion is over the DB state itself, not a rendered view.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT seq FROM bridge_messages WHERE team_id=? AND recipient=? ORDER BY seq",
            (team_id, recipient),
        ).fetchall()
    finally:
        conn.close()
    seqs = [r[0] for r in rows]
    assert len(seqs) == expected_total, (
        f"row count mismatch: expected {expected_total}, got {len(seqs)} "
        f"(missing rows imply a silently-dropped INSERT)"
    )
    expected = list(range(1, expected_total + 1))
    assert seqs == expected, (
        "per-recipient seq stream is not gap-free + monotonic from 1; "
        f"first divergence at index {next((i for i, (a, b) in enumerate(zip(seqs, expected, strict=False)) if a != b), 'N/A')}"
    )


def _assert_per_sender_fifo(per_sender_seqs: dict[str, list[int]]) -> None:
    """Oracle (2): each sender's subsequence is strictly increasing."""
    for sender, seqs in per_sender_seqs.items():
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1], (
                f"per-sender FIFO violation for {sender!r}: "
                f"seq[{i}]={seqs[i]} <= seq[{i - 1}]={seqs[i - 1]} "
                f"(send-call order was not preserved on the wire)"
            )


def _assert_cursor_replay(db_path: str, team_id: str, recipient: str, total: int) -> None:
    """Oracle (3): read_once from cursor=k returns exactly total-k rows.

    Probed at k ∈ {0, total//2, total-1, total} to catch off-by-one and
    boundary-condition bugs in the ``seq > ?`` predicate.
    """
    for k in (0, total // 2, total - 1, total):
        rows = read_once(
            db_path,
            team_id=team_id,
            role_id=recipient,
            since_seq=k,
            limit=total + 100,
            update_cursor=False,
        )
        assert len(rows) == total - k, (
            f"cursor replay from seq>{k}: expected {total - k} rows, got {len(rows)}"
        )
        if rows:
            # And the returned rows must themselves be strictly seq-ordered.
            seqs = [r["seq"] for r in rows]
            assert seqs == sorted(seqs)
            assert seqs[0] == k + 1


# ── Parametrized stress matrix ────────────────────────────────────────────


def _dispatch_writers(
    db_path: str,
    *,
    n_writers: int,
    scheduler: str,
    msgs_per_writer: int,
    rng: random.Random,
) -> list[tuple[str, list[int], list[float]]]:
    """Spawn ``n_writers`` x ``msgs_per_writer`` against one recipient.

    Sender→writer mapping is shuffled with the seeded RNG so successive
    runs hit the same (but non-trivial) assignment, exercising more of
    the per-sender FK edge than a sorted assignment would.
    """
    sender_ids = [f"sender-{i:02d}" for i in range(n_writers)]
    rng.shuffle(sender_ids)
    args_list = [
        (db_path, "T1", "team-lead", sender_ids[i], msgs_per_writer) for i in range(n_writers)
    ]

    if scheduler == "thread":
        with ThreadPoolExecutor(max_workers=n_writers) as ex:
            return list(ex.map(_writer_burst, args_list))

    if scheduler == "multiproc":
        # spawn context, not fork: SQLite + WAL behaves better when each
        # child opens its own handle from scratch, and spawn is the only
        # context that works uniformly across macOS + Linux in CI.
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=n_writers) as pool:
            return pool.map(_writer_burst, args_list)

    raise ValueError(f"unknown scheduler: {scheduler!r}")


@pytest.mark.parametrize("n_writers", N_WRITERS_MATRIX)
@pytest.mark.parametrize("scheduler", SCHEDULERS)
def test_concurrent_writers_preserve_invariants(
    team_db: str, n_writers: int, scheduler: str
) -> None:
    """Stress matrix: N ∈ {2,8,32} x {threads, multiproc} x 200 msgs.

    Asserts oracles (1) gap-free + monotonic seq, (2) per-sender FIFO,
    (3) cursor-replay-from-k. Idempotency identity (oracle 4) is covered
    by its own dedicated test below — that property does not need the
    stress matrix to falsify it.
    """
    rng = random.Random(RNG_SEED)
    results = _dispatch_writers(
        team_db,
        n_writers=n_writers,
        scheduler=scheduler,
        msgs_per_writer=MSGS_PER_WRITER,
        rng=rng,
    )

    total = n_writers * MSGS_PER_WRITER
    per_sender_seqs: dict[str, list[int]] = {}
    all_latencies: list[float] = []
    for sender, seqs, lats in results:
        # A single sender appears at most once in this matrix (one writer
        # per sender_id); collapsing into a dict catches bugs where a
        # writer mistakenly used another writer's sender_id.
        assert sender not in per_sender_seqs, (
            f"sender_id {sender!r} appeared in two writer results — "
            "test harness assigned the same sender twice"
        )
        per_sender_seqs[sender] = seqs
        all_latencies.extend(lats)

    # Oracle (1): per-recipient seq set is gap-free, no dup.
    _assert_no_gap_no_dup(team_db, "T1", "team-lead", expected_total=total)

    # Oracle (2): per-sender FIFO.
    _assert_per_sender_fifo(per_sender_seqs)

    # Oracle (3): cursor replay.
    _assert_cursor_replay(team_db, "T1", "team-lead", total=total)

    # Bench (no assertions — artifact only).
    _BENCH[f"n{n_writers:02d}-{scheduler}"] = all_latencies


# ── Oracle 4: idempotency replay returns the original tuple ───────────────


def test_idempotency_replay_returns_original_seq_and_persona(team_db: str) -> None:
    """Oracle (4): replay with the same ULID returns the *same*
    ``(seq, persona_snapshot_id)`` tuple — equality, not just non-error.

    Identity-preservation here is what stops a retry from silently
    re-stamping a message under a newer persona snapshot.
    """
    ulid = "01H8XGJWBWBAJ1ABCDEFGHIJKL"  # 26 chars, conforms to ULID_LEN
    assert len(ulid) == 26
    first = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="sender-00",
        kind="reply",
        payload="idem-A",
        idempotency_key=ulid,
    )
    second = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="sender-00",
        kind="reply",
        payload="idem-B-IGNORED",
        idempotency_key=ulid,
    )
    third = send(
        team_db,
        team_id="T1",
        recipient="team-lead",
        sender_id="sender-00",
        kind="reply",
        payload="idem-C-IGNORED",
        idempotency_key=ulid,
    )
    assert first["deduped"] is False
    assert second["deduped"] is True
    assert third["deduped"] is True
    assert second["seq"] == first["seq"]
    assert third["seq"] == first["seq"]
    assert second["persona_snapshot_id"] == first["persona_snapshot_id"]
    assert third["persona_snapshot_id"] == first["persona_snapshot_id"]

    # And the log carries exactly one row for that key — proves dedupe
    # did not silently overwrite.
    conn = sqlite3.connect(team_db)
    try:
        rows = conn.execute(
            "SELECT payload FROM bridge_messages WHERE idempotency_key=?",
            (ulid,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "idem-A"


def test_idempotency_replay_under_concurrency(team_db: str) -> None:
    """Concurrent dedupe: 8 threads racing the same key produce 1 row.

    Folds in the ai-safety adversarial fixture (idempotency-race): every
    racer must observe the same ``(seq, persona_snapshot_id)`` tuple,
    and the resulting log must contain exactly one row for the key.
    Catches the race between the fast-path SELECT and the INSERT in
    ``send()``.
    """
    ulid = "01H8XGJWBWBAJ1ABCDEFGHIJKM"
    n_racers = 8
    args = (
        team_db,
        "T1",
        "team-lead",
        "sender-00",
        "reply",
        "idem-race",
        ulid,
    )

    def _race(_i: int) -> dict:
        return send(
            args[0],
            team_id=args[1],
            recipient=args[2],
            sender_id=args[3],
            kind=args[4],
            payload=args[5],
            idempotency_key=args[6],
        )

    with ThreadPoolExecutor(max_workers=n_racers) as ex:
        results = list(ex.map(_race, range(n_racers)))

    # All racers must agree on the same (seq, persona_snapshot_id).
    seqs = {r["seq"] for r in results}
    snaps = {r["persona_snapshot_id"] for r in results}
    assert len(seqs) == 1, f"idempotency race produced multiple seqs: {seqs}"
    assert len(snaps) == 1, f"idempotency race produced multiple snapshots: {snaps}"

    # Exactly one row for the key.
    conn = sqlite3.connect(team_db)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM bridge_messages WHERE idempotency_key=?",
            (ulid,),
        ).fetchall()
    finally:
        conn.close()
    assert rows[0][0] == 1


# ── Chaos hook: SIGKILL mid-transaction ───────────────────────────────────


def test_sigkill_writer_mid_tx_no_torn_seq(team_db: str) -> None:
    """Chaos: SIGKILL a writer subprocess mid-loop; assert no torn seq.

    Verifies the WAL + ``synchronous=NORMAL`` durability claim — a
    writer killed inside ``BEGIN IMMEDIATE`` must leave the log in a
    state where (a) the seq set is still ``range(1, len+1)`` with no
    gap and no phantom row, (b) a fresh reader can still open the DB
    (no held write lock leaked from the dead child).
    """
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(
        target=_chaos_writer_loop,
        args=(team_db, "T1", "team-lead", "sender-00"),
    )
    p.start()
    try:
        # Let the writer land several rows before SIGKILL. We previously
        # slept 50 ms, but on slow / contended CI runners spawn() startup +
        # first connection open can swallow most of that window, leaving
        # seqs empty and making the chaos assertion vacuous (see M9). 300
        # ms is a conservative floor that still keeps the test sub-second
        # while comfortably clearing process startup on every CI runner
        # we ship on.
        time.sleep(0.3)
        assert p.pid is not None
        os.kill(p.pid, signal.SIGKILL)
        p.join(timeout=5)
        assert not p.is_alive(), "child survived SIGKILL — test is unreliable"
    finally:
        if p.is_alive():
            p.terminate()
            p.join(timeout=1)

    # (a) seq set is still contiguous from 1 — no torn write, no phantom.
    conn = sqlite3.connect(team_db)
    try:
        rows = conn.execute(
            "SELECT seq FROM bridge_messages WHERE team_id=? AND recipient=? ORDER BY seq",
            ("T1", "team-lead"),
        ).fetchall()
    finally:
        conn.close()
    seqs = [r[0] for r in rows]
    # Chaos cycle must have actually landed something — otherwise this
    # test is vacuously green and proves nothing about SIGKILL safety.
    assert len(seqs) > 0, (
        "SIGKILL fired before any row committed — chaos cycle landed nothing. "
        "This test is meaningless when seqs is empty. "
        "If this fails intermittently, extend the pre-SIGKILL sleep window."
    )
    # If any landed, they must be contiguous starting at 1 (gap-free,
    # no duplicate, no torn / phantom row).
    assert seqs == list(range(1, len(seqs) + 1)), (
        f"SIGKILL produced a torn / phantom seq stream: {seqs[:10]}..."
    )

    # (b) Fresh reader still works — no held lock leaked from the
    # dead child (busy_timeout would otherwise fire).
    fresh = read_once(
        team_db,
        team_id="T1",
        role_id="team-lead",
        since_seq=0,
        limit=10000,
        update_cursor=False,
    )
    assert len(fresh) == len(seqs)


# ── Reader cursor monotonicity across "sessions" ──────────────────────────


def test_reader_cursor_monotonic_across_sessions(team_db: str) -> None:
    """Reader cursor advances monotonically across interleaved opens.

    Simulates a reader that closes and re-opens between batches (the
    real follow-loop in ``bridge_read._follow`` does exactly this on
    each poll). The bridge_delivery row must reflect the last-seen seq
    and never regress, even when interleaved with new writes.
    """
    rng = random.Random(RNG_SEED)
    # Seed 100 rows in three batches, reading after each.
    for batch in range(3):
        for i in range(100):
            send(
                team_db,
                team_id="T1",
                recipient="team-lead",
                sender_id=f"sender-{(batch * 100 + i) % 32:02d}",
                kind="reply",
                payload=f"b{batch}-i{i}-{rng.random():.4f}",
            )
        # Read with cursor advance.
        rows = read_once(
            team_db,
            team_id="T1",
            role_id="team-lead",
            since_seq=batch * 100,
            limit=10_000,
            update_cursor=True,
        )
        assert len(rows) == 100

    # bridge_delivery should reflect the final cursor (=300) and nothing
    # less, regardless of how many opens happened in between.
    conn = sqlite3.connect(team_db)
    try:
        row = conn.execute(
            "SELECT last_seq FROM bridge_delivery WHERE team_id=? AND recipient=?",
            ("T1", "team-lead"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 300, f"reader cursor regressed: {row[0]}"
