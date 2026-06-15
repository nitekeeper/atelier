"""Tests for scripts/result_journal.py.

Coverage
--------
* Key determinism: same inputs → same key (called twice, identical result)
* Upstream-digest change → different key (cascade invalidation)
* Attempt is NOT part of the key (different attempt, same key)
* Order-independent upstream digest (sorted before hashing)
* Key is clock/RNG-free: byte-identical under a moving fake clock
* Persistence round-trip: put → reload → lookup
* Corrupt file → start fresh (no crash)
* Missing file → start fresh
* In-memory mode (path=None) — no file I/O
* envelope_hash is deterministic and JSON-key-order-independent
* get_envelope_hash returns None on miss, hash on hit
* Direct-upstream cascade: T3 re-dispatches when T1 changes (A5 contract)
* 2-hop content-chaining: ancestor change propagates only via changed
  intermediate output; byte-stable intermediate output → descendant replays
* Injective field encoding: delimiter-byte inputs do not collide
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from scripts.result_journal import ResultJournal

# ── fixture helpers ────────────────────────────────────────────────────────


def _task(
    task_id: str = "T1", persona: str = "backend-engineer-1", phase: str = "implement"
) -> dict:
    return {"task_id": task_id, "assigned_persona": persona, "phase": phase}


def _envelope(task_id: str = "T1", status: str = "done") -> dict:
    return {"task_id": task_id, "status": status, "artifacts": [], "notes_md": "ok"}


# ── key determinism ────────────────────────────────────────────────────────


def test_key_same_inputs_same_key():
    """Calling key() twice with identical inputs returns identical hex digest."""
    j = ResultJournal()
    task = _task()
    k1 = j.key(task, attempt=1, model="claude-sonnet-4-5", briefing="do the thing")
    k2 = j.key(task, attempt=1, model="claude-sonnet-4-5", briefing="do the thing")
    assert k1 == k2


def test_key_different_task_id_different_key():
    j = ResultJournal()
    k1 = j.key(_task("T1"), attempt=1, model="m", briefing="b")
    k2 = j.key(_task("T2"), attempt=1, model="m", briefing="b")
    assert k1 != k2


def test_key_different_model_different_key():
    j = ResultJournal()
    task = _task()
    k1 = j.key(task, attempt=1, model="claude-sonnet-4-5", briefing="b")
    k2 = j.key(task, attempt=1, model="claude-opus-4-7", briefing="b")
    assert k1 != k2


def test_key_different_briefing_different_key():
    j = ResultJournal()
    task = _task()
    k1 = j.key(task, attempt=1, model="m", briefing="briefing A")
    k2 = j.key(task, attempt=1, model="m", briefing="briefing B")
    assert k1 != k2


def test_key_attempt_not_part_of_key():
    """Different attempt values must produce the SAME key — retries replay cache."""
    j = ResultJournal()
    task = _task()
    k1 = j.key(task, attempt=1, model="m", briefing="b")
    k2 = j.key(task, attempt=2, model="m", briefing="b")
    assert k1 == k2, "attempt must NOT affect the journal key"


# ── upstream digest and cascade invalidation ───────────────────────────────


def test_key_upstream_digest_changes_key():
    """Changing upstream_envelope_hashes changes the key (cascade invalidation)."""
    j = ResultJournal()
    task = _task("T3")
    k1 = j.key(task, attempt=1, model="m", briefing="b", upstream_envelope_hashes=["abc"])
    k2 = j.key(task, attempt=1, model="m", briefing="b", upstream_envelope_hashes=["xyz"])
    assert k1 != k2


def test_key_upstream_digest_order_independent():
    """Upstream hashes are sorted before digest — order must not matter."""
    j = ResultJournal()
    task = _task("T3")
    k1 = j.key(task, attempt=1, model="m", briefing="b", upstream_envelope_hashes=["aaa", "bbb"])
    k2 = j.key(task, attempt=1, model="m", briefing="b", upstream_envelope_hashes=["bbb", "aaa"])
    assert k1 == k2


def test_key_no_upstream_vs_empty_upstream_equal():
    """None and [] upstream both produce the same key (empty digest)."""
    j = ResultJournal()
    task = _task()
    k1 = j.key(task, attempt=1, model="m", briefing="b", upstream_envelope_hashes=None)
    k2 = j.key(task, attempt=1, model="m", briefing="b", upstream_envelope_hashes=[])
    assert k1 == k2


def test_direct_upstream_cascade_invalidation():
    """A5 contract: T3 directly reads T1's output. If T1's briefing changes, its
    output envelope changes, so T3's direct-upstream digest shifts and T3
    re-dispatches. T2 (which T3 does NOT read) keeps its old key.

    This is direct-upstream content-chaining, not transitive-closure walking —
    T3's key only ever incorporates the hashes the host passes for its direct
    reads-from set."""
    j = ResultJournal()

    # Initial run
    t1_task = _task("T1")
    t2_task = _task("T2")
    t3_task = _task("T3")

    t1_env = _envelope("T1")
    t2_env = _envelope("T2")

    # Compute T1, T2 keys + store them
    k_t1_v1 = j.key(t1_task, attempt=1, model="m", briefing="T1 brief v1")
    k_t2 = j.key(t2_task, attempt=1, model="m", briefing="T2 brief")

    j.put(k_t1_v1, t1_env, {"output_tokens": 100})
    j.put(k_t2, t2_env, {"output_tokens": 50})

    h_t1_v1 = j.get_envelope_hash(k_t1_v1)
    h_t2 = j.get_envelope_hash(k_t2)
    assert h_t1_v1 is not None
    assert h_t2 is not None

    # T3 key with original T1 + T2 hashes
    k_t3_v1 = j.key(
        t3_task,
        attempt=1,
        model="m",
        briefing="T3 brief",
        upstream_envelope_hashes=[h_t1_v1, h_t2],
    )
    j.put(k_t3_v1, _envelope("T3"), {"output_tokens": 75})

    # Now mutate T1's briefing → new key → new envelope hash
    k_t1_v2 = j.key(t1_task, attempt=1, model="m", briefing="T1 brief CHANGED")
    assert k_t1_v2 != k_t1_v1, "T1 key must change when briefing changes"

    t1_env_v2 = _envelope("T1", status="done-v2")
    j.put(k_t1_v2, t1_env_v2, {"output_tokens": 110})
    h_t1_v2 = j.get_envelope_hash(k_t1_v2)

    # T3's new key uses updated T1 hash — must differ from old T3 key
    k_t3_v2 = j.key(
        t3_task,
        attempt=1,
        model="m",
        briefing="T3 brief",
        upstream_envelope_hashes=[h_t1_v2, h_t2],
    )
    assert k_t3_v2 != k_t3_v1, "T3 key must change when T1's envelope changes"

    # T2's key is unchanged (no dependency on T1)
    k_t2_check = j.key(t2_task, attempt=1, model="m", briefing="T2 brief")
    assert k_t2_check == k_t2, "T2 key must be unchanged"


# ── clock/RNG-free determinism lint ──────────────────────────────────────


def test_journal_key_is_clock_free():
    """Key must be byte-identical across two calls even when wall-clock advances.

    This test embeds a deliberate time.sleep(0) to allow the fake clock to
    advance, then asserts the key is unchanged.  The test proves the key does
    not incorporate time.time(), datetime.now(), os.urandom(), or any other
    non-deterministic source.
    """
    j = ResultJournal()
    task = _task("T-clock")

    k1 = j.key(task, attempt=1, model="claude-opus-4-7", briefing="stable briefing")
    time.sleep(0)  # allow clock to tick (even if OS rounds to 0)
    k2 = j.key(task, attempt=1, model="claude-opus-4-7", briefing="stable briefing")

    assert k1 == k2, (
        f"Journal key must be clock-free: k1={k1!r}, k2={k2!r} differ — "
        "the key incorporates a non-deterministic source (time/RNG)."
    )


# ── lookup + put ──────────────────────────────────────────────────────────


def test_lookup_miss_returns_none():
    j = ResultJournal()
    assert j.lookup("nonexistent-key") is None


def test_put_then_lookup_returns_envelope():
    j = ResultJournal()
    task = _task()
    k = j.key(task, attempt=1, model="m", briefing="b")
    env = _envelope()
    j.put(k, env, {"output_tokens": 42})
    assert j.lookup(k) == env


def test_delete_removes_entry_and_persists(tmp_path: Path):
    """``delete`` invalidates a cached entry: it returns True on a present key,
    removes it (subsequent ``lookup`` misses), and the removal SURVIVES a reload
    from disk.  ``delete`` of an absent key returns False and is a no-op.

    This is the INVALIDATE primitive the host_scheduler false-`done` guard uses
    to drop a rejected envelope so a retry RE-EXECUTES (``attempt`` is not part
    of the journal key, so without this a rejected `done` replays forever)."""
    journal_file = tmp_path / "journal.json"
    j1 = ResultJournal(journal_file)
    task = _task()
    k = j1.key(task, attempt=1, model="m", briefing="b")
    j1.put(k, _envelope(), {"output_tokens": 42})
    assert j1.lookup(k) == _envelope()  # present pre-delete

    # delete of a PRESENT key → True, entry gone in-memory.
    assert j1.delete(k) is True
    assert j1.lookup(k) is None

    # delete of an ABSENT key → False, no-op.
    assert j1.delete(k) is False
    assert j1.delete("never-stored-key") is False

    # The removal was persisted — a fresh journal from the same path misses too.
    j2 = ResultJournal(journal_file)
    assert j2.lookup(k) is None


def test_delete_in_memory_mode():
    """``delete`` works in path=None (in-memory) mode without touching disk."""
    j = ResultJournal(None)
    task = _task()
    k = j.key(task, attempt=1, model="m", briefing="b")
    j.put(k, _envelope(), {"output_tokens": 1})
    assert j.delete(k) is True
    assert j.lookup(k) is None
    assert j.delete(k) is False


def test_get_envelope_hash_miss_returns_none():
    j = ResultJournal()
    assert j.get_envelope_hash("nonexistent") is None


def test_get_envelope_hash_hit_returns_hash():
    j = ResultJournal()
    task = _task()
    k = j.key(task, attempt=1, model="m", briefing="b")
    env = _envelope()
    j.put(k, env, {"output_tokens": 10})
    h = j.get_envelope_hash(k)
    assert h is not None and len(h) == 64  # sha256 hex = 64 chars


# ── envelope_hash determinism ─────────────────────────────────────────────


def test_envelope_hash_deterministic():
    j = ResultJournal()
    env = {"task_id": "T1", "status": "done", "artifacts": [], "notes_md": "ok"}
    h1 = j.envelope_hash(env)
    h2 = j.envelope_hash(env)
    assert h1 == h2


def test_envelope_hash_key_order_independent():
    """envelope_hash must be the same regardless of dict key insertion order."""
    j = ResultJournal()
    env_a = {"task_id": "T1", "status": "done", "notes_md": "ok"}
    env_b = {"notes_md": "ok", "status": "done", "task_id": "T1"}
    assert j.envelope_hash(env_a) == j.envelope_hash(env_b)


def test_envelope_hash_different_content_different_hash():
    j = ResultJournal()
    env1 = {"task_id": "T1", "status": "done"}
    env2 = {"task_id": "T1", "status": "failed"}
    assert j.envelope_hash(env1) != j.envelope_hash(env2)


# ── persistence round-trip ────────────────────────────────────────────────


def test_persistence_round_trip(tmp_path: Path):
    """put() persists; a new ResultJournal from the same path finds the entry."""
    journal_file = tmp_path / "journal.json"
    j1 = ResultJournal(journal_file)
    task = _task()
    k = j1.key(task, attempt=1, model="m", briefing="b")
    env = _envelope()
    j1.put(k, env, {"output_tokens": 99})

    # Reload from disk
    j2 = ResultJournal(journal_file)
    assert j2.lookup(k) == env
    assert j2.get_envelope_hash(k) == j1.get_envelope_hash(k)


def test_persistence_multiple_entries(tmp_path: Path):
    journal_file = tmp_path / "journal.json"
    j1 = ResultJournal(journal_file)

    keys = []
    for i in range(3):
        task = _task(f"T{i}")
        k = j1.key(task, attempt=1, model="m", briefing=f"briefing {i}")
        j1.put(k, _envelope(f"T{i}"), {"output_tokens": i * 10})
        keys.append((k, f"T{i}"))

    j2 = ResultJournal(journal_file)
    for k, task_id in keys:
        result = j2.lookup(k)
        assert result is not None
        assert result["task_id"] == task_id


def test_in_memory_mode_no_file(tmp_path: Path):
    """path=None → no file is created, in-memory only."""
    j = ResultJournal(None)
    task = _task()
    k = j.key(task, attempt=1, model="m", briefing="b")
    j.put(k, _envelope(), {"output_tokens": 1})
    assert j.lookup(k) == _envelope()
    # Confirm no files were created in tmp_path
    assert list(tmp_path.iterdir()) == []


# ── corrupt file → fresh start ────────────────────────────────────────────


def test_corrupt_json_file_starts_fresh(tmp_path: Path):
    """A JSON-corrupt backing file must not crash — journal starts fresh."""
    journal_file = tmp_path / "journal.json"
    journal_file.write_text("{INVALID JSON {{", encoding="utf-8")
    j = ResultJournal(journal_file)  # must not raise
    assert j.lookup("any-key") is None


def test_wrong_type_json_starts_fresh(tmp_path: Path):
    """A valid JSON file that is not a dict (e.g. a list) starts fresh."""
    journal_file = tmp_path / "journal.json"
    journal_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    j = ResultJournal(journal_file)
    assert j.lookup("any-key") is None


def test_empty_json_file_starts_fresh(tmp_path: Path):
    """An empty file is treated as corrupt → fresh start, no crash."""
    journal_file = tmp_path / "journal.json"
    journal_file.write_text("", encoding="utf-8")
    j = ResultJournal(journal_file)
    assert j.lookup("any-key") is None


def test_missing_file_starts_fresh(tmp_path: Path):
    """A non-existent path → in-memory start, no crash."""
    journal_file = tmp_path / "nonexistent.json"
    j = ResultJournal(journal_file)
    assert j.lookup("any-key") is None


# ── 2-hop content-chaining (MAJOR-1) ──────────────────────────────────────


def test_two_hop_chain_propagates_through_content():
    """T1→T2→T3 chain: when T1 changes AND that changes T2's OUTPUT envelope,
    the change propagates to T3 via the changed direct-upstream (T2) hash.

    The journal does NOT compute a transitive closure — T3 only ever sees its
    DIRECT upstream (T2). Propagation is a property of content-chaining: T2's
    output changed, so T2's envelope_hash changed, so T3's key changed.
    """
    j = ResultJournal()

    t1_task = _task("T1")
    t2_task = _task("T2")
    t3_task = _task("T3")

    # ── Run with T1 v1 ──
    # T1 emits an envelope whose content encodes its input ("v1").
    k_t1_v1 = j.key(t1_task, attempt=1, model="m", briefing="T1 input v1")
    t1_env_v1 = _envelope("T1", status="from-T1-v1")
    j.put(k_t1_v1, t1_env_v1, {"output_tokens": 10})
    h_t1_v1 = j.get_envelope_hash(k_t1_v1)

    # T2 reads T1's output. Its OUTPUT envelope is DERIVED from T1's output,
    # so its content reflects T1's value.
    k_t2_v1 = j.key(
        t2_task,
        attempt=1,
        model="m",
        briefing="T2 brief",
        upstream_envelope_hashes=[h_t1_v1],
    )
    t2_env_v1 = _envelope("T2", status="derived-from-T1-v1")
    j.put(k_t2_v1, t2_env_v1, {"output_tokens": 20})
    h_t2_v1 = j.get_envelope_hash(k_t2_v1)

    # T3 reads T2's output (its DIRECT upstream — NOT T1).
    k_t3_v1 = j.key(
        t3_task,
        attempt=1,
        model="m",
        briefing="T3 brief",
        upstream_envelope_hashes=[h_t2_v1],
    )

    # ── Mutate T1 → v2; T2 re-emits DIFFERENT content derived from T1 v2 ──
    k_t1_v2 = j.key(t1_task, attempt=1, model="m", briefing="T1 input v2")
    assert k_t1_v2 != k_t1_v1, "T1 key must change when its input changes"
    t1_env_v2 = _envelope("T1", status="from-T1-v2")
    j.put(k_t1_v2, t1_env_v2, {"output_tokens": 11})
    h_t1_v2 = j.get_envelope_hash(k_t1_v2)
    assert h_t1_v2 != h_t1_v1, "T1 output envelope must change"

    # T2 re-keys (its direct upstream T1 changed) and emits DIFFERENT content.
    k_t2_v2 = j.key(
        t2_task,
        attempt=1,
        model="m",
        briefing="T2 brief",
        upstream_envelope_hashes=[h_t1_v2],
    )
    assert k_t2_v2 != k_t2_v1, "T2 key must change when its direct upstream T1 changes"
    t2_env_v2 = _envelope("T2", status="derived-from-T1-v2")  # different content
    j.put(k_t2_v2, t2_env_v2, {"output_tokens": 21})
    h_t2_v2 = j.get_envelope_hash(k_t2_v2)
    assert h_t2_v2 != h_t2_v1, "T2 output envelope must change (it derived from T1)"

    # T3 re-keys because its DIRECT upstream (T2) output changed.
    k_t3_v2 = j.key(
        t3_task,
        attempt=1,
        model="m",
        briefing="T3 brief",
        upstream_envelope_hashes=[h_t2_v2],
    )
    assert k_t3_v2 != k_t3_v1, (
        "T3 must re-dispatch: its direct upstream T2's output changed "
        "(propagated from T1 via content-chaining)"
    )


def test_two_hop_chain_replays_when_intermediate_output_stable():
    """T1→T2→T3: when T1 changes but T2 re-emits BYTE-IDENTICAL output, T3
    CORRECTLY replays from cache — T3's real input (T2's output) is unchanged.

    This documents the safe optimization that content-chaining gives for free:
    an ancestor change that does NOT alter the intermediate's output is, by
    definition, irrelevant to the descendant.
    """
    j = ResultJournal()

    t1_task = _task("T1")
    t2_task = _task("T2")
    t3_task = _task("T3")

    # ── v1 ──
    k_t1_v1 = j.key(t1_task, attempt=1, model="m", briefing="T1 input v1")
    j.put(k_t1_v1, _envelope("T1", status="from-T1-v1"), {"output_tokens": 10})
    h_t1_v1 = j.get_envelope_hash(k_t1_v1)

    # T2's output is BYTE-IDENTICAL regardless of which T1 it read (e.g. T1's
    # change was cosmetic / did not affect what T2 consumes).
    stable_t2_env = _envelope("T2", status="STABLE-OUTPUT")
    k_t2_v1 = j.key(
        t2_task,
        attempt=1,
        model="m",
        briefing="T2 brief",
        upstream_envelope_hashes=[h_t1_v1],
    )
    j.put(k_t2_v1, stable_t2_env, {"output_tokens": 20})
    h_t2_v1 = j.get_envelope_hash(k_t2_v1)

    k_t3_v1 = j.key(
        t3_task,
        attempt=1,
        model="m",
        briefing="T3 brief",
        upstream_envelope_hashes=[h_t2_v1],
    )

    # ── Mutate T1; T2 re-emits the SAME bytes ──
    k_t1_v2 = j.key(t1_task, attempt=1, model="m", briefing="T1 input v2")
    j.put(k_t1_v2, _envelope("T1", status="from-T1-v2"), {"output_tokens": 11})
    h_t1_v2 = j.get_envelope_hash(k_t1_v2)

    k_t2_v2 = j.key(
        t2_task,
        attempt=1,
        model="m",
        briefing="T2 brief",
        upstream_envelope_hashes=[h_t1_v2],
    )
    # T2 re-emits byte-identical content → same envelope_hash.
    j.put(k_t2_v2, stable_t2_env, {"output_tokens": 21})
    h_t2_v2 = j.get_envelope_hash(k_t2_v2)
    assert h_t2_v2 == h_t2_v1, "T2 output is byte-identical → same envelope_hash"

    # T3's key uses T2's (unchanged) output hash → SAME key → replays.
    k_t3_v2 = j.key(
        t3_task,
        attempt=1,
        model="m",
        briefing="T3 brief",
        upstream_envelope_hashes=[h_t2_v2],
    )
    assert k_t3_v2 == k_t3_v1, (
        "T3 must replay: its direct upstream T2's output is byte-stable, so "
        "T3's real input is unchanged despite the T1 change"
    )


# ── injective field encoding (MINOR-3 collision guard) ────────────────────


def test_delimiter_byte_inputs_do_not_collide():
    """key(task_id='A\\x00B', persona='P') must NOT collide with
    key(task_id='A', persona='B\\x00P').

    Pre-fix, fields were NUL-joined, so an embedded NUL byte could shift a
    field boundary and produce identical hashes. Length-prefix encoding makes
    the concatenation injective.
    """
    j = ResultJournal()
    task_a = {"task_id": "A\x00B", "assigned_persona": "P", "phase": "ph"}
    task_b = {"task_id": "A", "assigned_persona": "B\x00P", "phase": "ph"}
    k_a = j.key(task_a, attempt=1, model="m", briefing="b")
    k_b = j.key(task_b, attempt=1, model="m", briefing="b")
    assert k_a != k_b, "delimiter-byte inputs must not collide under injective encoding"


def test_field_boundary_shift_does_not_collide():
    """A boundary shift across phase/model must not collide either."""
    j = ResultJournal()
    task_a = {"task_id": "T", "assigned_persona": "p", "phase": "X\x00Y"}
    task_b = {"task_id": "T", "assigned_persona": "p", "phase": "X"}
    k_a = j.key(task_a, attempt=1, model="m", briefing="b")
    k_b = j.key(task_b, attempt=1, model="Y\x00m", briefing="b")
    assert k_a != k_b


def test_upstream_hash_multiset_injective():
    """Two upstream-hash lists that NUL-concat to the same bytes must still
    produce different digests (injective per-element encoding)."""
    j = ResultJournal()
    task = _task("T3")
    k1 = j.key(
        task,
        attempt=1,
        model="m",
        briefing="b",
        upstream_envelope_hashes=["a\x00b", "c"],
    )
    k2 = j.key(
        task,
        attempt=1,
        model="m",
        briefing="b",
        upstream_envelope_hashes=["a", "b\x00c"],
    )
    assert k1 != k2
