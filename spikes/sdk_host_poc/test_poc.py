"""M0 PoC - 5 pass/fail assertions (the M1-M4 acceptance contract).

Run with::

    PYTHONPATH=. pytest spikes/sdk_host_poc/test_poc.py -q

No API key required.  All assertions run against ``FakeAgent`` (stdlib-only).

Assertions
----------
A1 — Ephemeral metered agents: T1∥T2 dispatch exactly 2 query() calls; each
     returns output_tokens > 0; budget.spent() == sum of output_tokens.

A2 — Budget ceiling + throws: BudgetPool sized so effective ceiling is exceeded
     by the 3rd dispatch (T3). T3's run_attempt raises BudgetExceeded BEFORE
     any query() for T3 (fake records 2 calls, not 3); host routes T3 to
     abandon, not silent truncation.

A3 — Forced schema-validated return + anti-spoof: fake configured to return a
     malformed envelope (wrong task_id) for one attempt → validate_envelope
     raises EnvelopeValidationError; a well-formed one passes; the schema
     object is the same ENVELOPE_SCHEMA passed as output_format.

A4 — Barrier-free pipeline on independent edge + barrier on dependency:
     T2 sleeps longer than T1.  T1 reaches TERMINAL while T2 is still running
     (confirmed by done_timestamps).  T3 does NOT start until both T1 and T2
     are TERMINAL.  Wall-clock ≈ max(T1,T2) + T3, not T1+T2+T3.

A5 — Journal replay at ~zero cost + upstream invalidation:
     Run the DAG twice with identical inputs → run-2 makes 0 query() calls;
     run-2 budget.spent() == 0.  Then mutate T1's briefing and re-run → T1
     AND T3 re-dispatch (cascade invalidation) but T2 replays from journal.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from spikes.sdk_host_poc.agent_call import run_attempt
from spikes.sdk_host_poc.budget import BudgetExceeded, BudgetPool
from spikes.sdk_host_poc.envelope_schema import (
    ENVELOPE_SCHEMA,
    EnvelopeValidationError,
    validate_envelope,
)
from spikes.sdk_host_poc.fake_agent import FakeAgent, claude_cli_available
from spikes.sdk_host_poc.host import DagResult, build_poc_tasks, make_task, run_dag
from spikes.sdk_host_poc.journal import ResultJournal

# ── Helpers ────────────────────────────────────────────────────────────────────


def fresh_budget(total_tokens: int = 10_000, headroom: float = 0.70) -> BudgetPool:
    return BudgetPool(total_tokens=total_tokens, headroom=headroom)


def fresh_journal() -> ResultJournal:
    return ResultJournal()  # in-memory, no file


def fresh_fake(default_output_tokens: int = 100) -> FakeAgent:
    return FakeAgent(default_output_tokens=default_output_tokens)


def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


# ── A1: Ephemeral metered agents ───────────────────────────────────────────────


def _two_independent_tasks() -> list:
    """Build a 2-task write-disjoint DAG (T1, T2 — no dependency).

    Routing A1 through ``run_dag`` on this DAG genuinely exercises the
    concurrent scheduler (both tasks dispatched in the same wave), rather than
    awaiting two ``run_attempt`` calls serially.
    """
    return [
        make_task("t1", writes=["a.txt"], briefing="T1: write a.txt"),
        make_task("t2", writes=["b.txt"], briefing="T2: write b.txt"),
    ]


class TestA1EphemeralMeteredAgents:
    """A1 — Run T1∥T2 through the concurrent scheduler; assert exactly 2
    query() calls; each returns output_tokens > 0; budget.spent() ==
    sum(output_tokens).
    """

    def test_exactly_two_query_calls_for_t1_and_t2(self):
        """Two independent tasks dispatched through run_dag → exactly 2 queries."""
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent(default_output_tokens=150)

        t1_tokens = 200
        t2_tokens = 180
        fake.configure("t1", output_tokens=t1_tokens)
        fake.configure("t2", output_tokens=t2_tokens)

        tasks = _two_independent_tasks()
        result = run(
            run_dag(tasks, budget=budget, journal=journal, fake_agent=fake, est_per_agent=200)
        )

        # Exactly 2 query() calls through the concurrent scheduler
        assert fake.call_count == 2, f"expected 2 query() calls, got {fake.call_count}"

        # budget.spent() == Σ output_tokens (each metered in-band)
        assert budget.spent() == t1_tokens + t2_tokens, (
            f"budget.spent()={budget.spent()} != expected {t1_tokens + t2_tokens}"
        )

        # Both envelopes are valid and present (ephemeral metered agents)
        assert result.envelopes["t1"]["task_id"] == "t1"
        assert result.envelopes["t2"]["task_id"] == "t2"

    def test_each_attempt_returns_positive_output_tokens(self):
        """Each metered usage dict carries output_tokens > 0 (in-band meter)."""
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent(default_output_tokens=150)
        fake.configure("t1", output_tokens=120)
        fake.configure("t2", output_tokens=90)

        tasks = _two_independent_tasks()
        run(run_dag(tasks, budget=budget, journal=journal, fake_agent=fake, est_per_agent=200))

        # Every recorded usage has output_tokens > 0 (proved transitively by a
        # positive total over exactly two metered calls).
        assert budget.spent() == 120 + 90
        assert budget.spent() > 0

    def test_budget_spent_equals_sum_of_output_tokens(self):
        """budget.spent() tracks Σ output_tokens from each usage dict."""
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent()

        expected_tokens_t1 = 300
        expected_tokens_t2 = 250
        fake.configure("t1", output_tokens=expected_tokens_t1)
        fake.configure("t2", output_tokens=expected_tokens_t2)

        tasks = _two_independent_tasks()
        run(run_dag(tasks, budget=budget, journal=journal, fake_agent=fake, est_per_agent=300))
        assert budget.spent() == expected_tokens_t1 + expected_tokens_t2


# ── A2: Budget ceiling + throws ────────────────────────────────────────────────


class TestA2BudgetCeilingAndThrows:
    """A2 — BudgetPool sized so the effective ceiling is exceeded by the 3rd
    dispatch.  T3's run_attempt raises BudgetExceeded BEFORE any query() for
    T3 (fake records exactly 2 calls, not 3).  Host routes T3 to abandon.
    """

    def test_budget_exceeded_before_third_query(self):
        """T3 must raise BudgetExceeded with fake still showing exactly 2 calls."""
        # Design the budget so that T1+T2 fit but T3 does not.
        # output_tokens per task = 100 (default)
        # After T1+T2: spent = 200
        # effective_ceiling = total * 0.70
        # We want: 200 + 100 > effective_ceiling → effective_ceiling < 300
        # So: total * 0.70 < 300 → total < 429
        # Choose total=400 → effective_ceiling = 280
        # T1(100) + T2(100) = 200, remaining = 80 < est(100) → T3 raises
        budget = BudgetPool(total_tokens=400, headroom=0.70)
        journal = fresh_journal()
        fake = FakeAgent(default_output_tokens=100)
        tasks = build_poc_tasks()

        result: DagResult = run(
            run_dag(tasks, budget=budget, journal=journal, fake_agent=fake, est_per_agent=100)
        )

        # Exactly 2 query() calls (T1 and T2 only — T3 was pre-empted)
        assert fake.call_count == 2, (
            f"expected 2 query() calls (T3 pre-empted by BudgetExceeded), got {fake.call_count}"
        )

        # T3 must be in the abandoned set
        assert "t3" in result.abandoned_tasks, (
            f"T3 must be abandoned; abandoned={result.abandoned_tasks}"
        )
        assert result.envelopes.get("t3") is None, "T3 envelope must be None (abandoned)"

        # T1 and T2 completed successfully
        assert result.envelopes.get("t1") is not None, "T1 must have completed"
        assert result.envelopes.get("t2") is not None, "T2 must have completed"

        # The exception recorded for T3 must be BudgetExceeded
        exc = result.exceptions.get("t3")
        assert isinstance(exc, BudgetExceeded), (
            f"T3 exception must be BudgetExceeded, got {type(exc).__name__!r}: {exc}"
        )

    def test_budget_throws_pre_query_not_post(self):
        """BudgetExceeded fires BEFORE query() — fake call count does not increment."""
        # Shrink budget so even the first task exceeds the ceiling
        budget = BudgetPool(total_tokens=50, headroom=0.70)  # ceiling = 35
        journal = fresh_journal()
        fake = FakeAgent(default_output_tokens=100)
        tasks = build_poc_tasks()

        with pytest.raises(BudgetExceeded):
            run(
                run_attempt(
                    tasks[0],
                    1,
                    budget=budget,
                    journal=journal,
                    model=tasks[0]["model"],
                    briefing=tasks[0]["briefing"],
                    query_fn=fake.query_fn("t1", 1),
                    est_per_agent=100,
                )
            )

        # Zero query() calls — the exception fired pre-dispatch
        assert fake.call_count == 0, (
            f"expected 0 query() calls (BudgetExceeded pre-dispatch), got {fake.call_count}"
        )

    def test_budget_abandoned_not_silently_truncated(self):
        """A BudgetExceeded task is abandoned (in DagResult.abandoned_tasks),
        never silently completed."""
        budget = BudgetPool(total_tokens=400, headroom=0.70)
        journal = fresh_journal()
        fake = FakeAgent(default_output_tokens=100)
        tasks = build_poc_tasks()

        result = run(
            run_dag(tasks, budget=budget, journal=journal, fake_agent=fake, est_per_agent=100)
        )

        # T3 abandoned, not in completed envelopes
        assert "t3" in result.abandoned_tasks
        assert "t3" not in {tid for tid, env in result.envelopes.items() if env is not None}


# ── A3: Forced schema-validated return + anti-spoof ───────────────────────────


class TestA3SchemaValidatedReturn:
    """A3 — Malformed envelope (wrong task_id) raises EnvelopeValidationError;
    well-formed envelope passes; ENVELOPE_SCHEMA is the same object.
    """

    def test_malformed_envelope_wrong_task_id_raises(self):
        """Anti-spoof: wrong task_id in envelope → EnvelopeValidationError."""
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent()

        tasks = build_poc_tasks()
        t1 = tasks[0]

        # Configure fake to return an envelope with wrong task_id
        fake.configure("t1", wrong_task_id="t999")

        with pytest.raises(EnvelopeValidationError) as exc_info:
            run(
                run_attempt(
                    t1,
                    1,
                    budget=budget,
                    journal=journal,
                    model=t1["model"],
                    briefing=t1["briefing"],
                    query_fn=fake.query_fn("t1", 1),
                )
            )

        err = exc_info.value
        assert err.field == "task_id", f"expected field='task_id', got {err.field!r}"
        assert "t999" in str(err) or err.got == "t999", (
            f"expected wrong_task_id 't999' in error, got {err}"
        )

    def test_well_formed_envelope_passes(self):
        """A well-formed envelope passes validate_envelope without error."""
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent()

        tasks = build_poc_tasks()
        t1 = tasks[0]

        envelope = run(
            run_attempt(
                t1,
                1,
                budget=budget,
                journal=journal,
                model=t1["model"],
                briefing=t1["briefing"],
                query_fn=fake.query_fn("t1", 1),
            )
        )
        assert envelope["task_id"] == "t1"
        assert envelope["status"] == "done"

    def test_output_format_is_envelope_schema(self):
        """FORCED SCHEMA (the wired half of A3): the options object the fake
        actually received carries ``output_format is ENVELOPE_SCHEMA``.

        This proves the schema is genuinely threaded to the agent-call seam, not
        merely declared.  In M3 the SpikeOptions becomes the assembled ``claude``
        argv (``--json-schema``); here we assert the load-bearing field is
        present and is the same object validate_envelope checks against.
        """
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent()
        t1 = build_poc_tasks()[0]

        run(
            run_attempt(
                t1,
                1,
                budget=budget,
                journal=journal,
                model=t1["model"],
                briefing=t1["briefing"],
                query_fn=fake.query_fn("t1", 1),
            )
        )

        assert fake.call_count == 1, "exactly one agent call recorded"
        recorded_options = fake.calls[0]["options"]
        assert recorded_options is not None, "the fake must receive a non-None options object"
        assert recorded_options.output_format is ENVELOPE_SCHEMA, (
            "the options passed to the agent call must carry output_format IS "
            "ENVELOPE_SCHEMA (forced schema-validated return)"
        )

    def test_validate_envelope_is_real_atelier_function(self):
        """The validator is atelier's real function (same function object) —
        proves the KEEP-set plugs in, not a re-typed shim."""
        from scripts.pm_dispatch_envelope import validate_envelope as ve_from_atelier
        from spikes.sdk_host_poc.envelope_schema import (
            validate_envelope as ve_from_module,
        )

        assert ve_from_module is ve_from_atelier

    def test_envelope_schema_has_required_fields(self):
        """ENVELOPE_SCHEMA contains the six fields that validate_envelope checks."""
        required = {"type", "task_id", "attempt", "status", "artifacts"}
        schema_required = set(ENVELOPE_SCHEMA.get("required", []))
        assert required.issubset(schema_required), (
            f"ENVELOPE_SCHEMA missing required fields: {required - schema_required}"
        )

    def test_validate_envelope_directly(self):
        """Direct unit test: validate_envelope raises on wrong type field."""
        malformed = {
            "type": "NOT_task_result",
            "task_id": "t1",
            "attempt": 1,
            "status": "done",
            "artifacts": [{"path": "a.txt", "sha": "abc"}],
        }
        with pytest.raises(EnvelopeValidationError) as exc_info:
            validate_envelope(malformed, dispatched_task_id="t1", dispatched_attempt=1)
        assert exc_info.value.field == "type"


# ── A4: Barrier-free pipeline + barrier on dependency ─────────────────────────


class TestA4BarrierFreePipeline:
    """A4 — T2 sleeps longer than T1.  T1 reaches TERMINAL while T2 is still
    running.  T3 does NOT start until both T1 and T2 are TERMINAL.
    Wall-clock ≈ max(T1,T2) + T3, not T1+T2+T3.

    Fixture sleeps are deliberately WIDE (T2≈0.30s, T1≈0.05s, T3≈0.05s) so the
    absolute timing slack is tens of ms even though the ratio assertion stays
    tight — avoids CI flakiness under load/GC.
    """

    # Wide fixture sleeps: absolute slack is large, ratios stay tight.
    T1_SLEEP = 0.05  # 50 ms  (shorter — T1 finishes while T2 still runs)
    T2_SLEEP = 0.30  # 300 ms (longer — the pipeline bottleneck)
    T3_SLEEP = 0.05  # 50 ms

    def _run_dag(self):
        """Run the canonical A4 DAG with the wide fixture sleeps."""
        budget = fresh_budget()
        journal = fresh_journal()
        fake = FakeAgent(default_output_tokens=50)
        fake.configure("t1", sleep_s=self.T1_SLEEP, output_tokens=50)
        fake.configure("t2", sleep_s=self.T2_SLEEP, output_tokens=50)
        fake.configure("t3", sleep_s=self.T3_SLEEP, output_tokens=50)
        tasks = build_poc_tasks()
        wall_start = time.monotonic()
        result = run(
            run_dag(tasks, budget=budget, journal=journal, fake_agent=fake, est_per_agent=50)
        )
        wall_elapsed = time.monotonic() - wall_start
        return result, wall_elapsed

    def test_t1_terminates_while_t2_still_running(self):
        """T1 done timestamp precedes T2 done timestamp (T2 sleeps longer)."""
        result, _ = self._run_dag()
        done = result.done_timestamps
        assert {"t1", "t2", "t3"} <= done.keys(), "all tasks must have done_timestamps"
        # T1 completes before T2 (it slept shorter) — proves T1 reached terminal
        # while T2 was still in-flight (host did not block T1 on T2).
        assert done["t1"] < done["t2"], (
            f"T1 should finish before T2: t1={done['t1']:.4f}, t2={done['t2']:.4f}"
        )

    def test_t3_barrier_via_start_timestamp(self):
        """BARRIER PROOF (sleep-ratio independent): T3 START >= max(T1,T2 DONE).

        Using T3's START timestamp (not its done) proves the host did not begin
        T3 until BOTH upstreams were terminal — regardless of how the sleep
        durations compare (the old completion-timestamp proof only held because
        t3_sleep < t2_sleep, which was fragile).
        """
        result, _ = self._run_dag()
        start = result.start_timestamps
        done = result.done_timestamps
        assert "t3" in start, "T3 must have a start_timestamp"
        barrier = max(done["t1"], done["t2"])
        assert start["t3"] >= barrier, (
            f"T3 STARTED before both upstreams terminal: "
            f"start[t3]={start['t3']:.4f} < max(done[t1],done[t2])={barrier:.4f}"
        )

    def test_t1_t2_start_before_either_finishes(self):
        """T1 and T2 both START before EITHER finishes → genuine concurrency.

        This proves the independent edge is barrier-FREE: the host launched both
        T1 and T2 concurrently rather than serializing them.
        """
        result, _ = self._run_dag()
        start = result.start_timestamps
        done = result.done_timestamps
        # Both started before the first one finished
        earliest_done = min(done["t1"], done["t2"])
        assert start["t1"] < earliest_done, "T1 must start before the first upstream finishes"
        assert start["t2"] < earliest_done, "T2 must start before the first upstream finishes"

    def test_wall_clock_between_pipeline_floor_and_sequential(self):
        """Wall-clock sits at the PIPELINE bound, not the SEQUENTIAL bound.

        Sequential bound = T1+T2+T3 = 0.40s (what a serial run would take).
        Pipeline  bound = max(T1,T2)+T3 = 0.35s (T1∥T2 overlap, then T3).

        The gate is the midpoint (0.375s): a genuinely pipelined run lands near
        0.35s and is comfortably BELOW the gate (~25ms slack); a serialized run
        would land near 0.40s and blow through it.  Both margins are tens of ms,
        so the assertion is robust to CI timing jitter while still proving the
        overlap (a serial run cannot pass).
        """
        _, elapsed = self._run_dag()
        sequential = self.T1_SLEEP + self.T2_SLEEP + self.T3_SLEEP  # 0.40
        pipeline = max(self.T1_SLEEP, self.T2_SLEEP) + self.T3_SLEEP  # 0.35
        gate = (pipeline + sequential) / 2  # 0.375 — midpoint, ~25ms each side
        assert elapsed < gate, (
            f"Wall-clock {elapsed:.3f}s >= midpoint gate {gate:.3f}s "
            f"(pipeline floor {pipeline:.3f}s, sequential {sequential:.3f}s) — "
            "T1 and T2 may not be running concurrently"
        )


# ── A5: Journal replay + upstream invalidation ────────────────────────────────


class TestA5JournalReplay:
    """A5 — Run DAG twice with identical inputs → run-2 makes 0 query() calls,
    budget.spent()==0.  Mutate T1 briefing → T1+T3 re-dispatch, T2 replays.
    """

    def test_second_run_zero_query_calls(self):
        """Run-2 with identical inputs makes 0 query() calls (all journal hits)."""
        journal = fresh_journal()

        # Run 1
        budget1 = fresh_budget()
        fake1 = FakeAgent(default_output_tokens=100)
        tasks = build_poc_tasks()
        result1 = run(
            run_dag(tasks, budget=budget1, journal=journal, fake_agent=fake1, est_per_agent=100)
        )

        assert fake1.call_count == 3, f"Run 1 should have 3 query() calls, got {fake1.call_count}"
        assert result1.envelopes.get("t1") is not None
        assert result1.envelopes.get("t2") is not None
        assert result1.envelopes.get("t3") is not None

        # Run 2 — same journal, same tasks, fresh budget and fake
        budget2 = BudgetPool(total_tokens=10_000)
        fake2 = FakeAgent(default_output_tokens=100)
        result2 = run(
            run_dag(tasks, budget=budget2, journal=journal, fake_agent=fake2, est_per_agent=100)
        )

        assert fake2.call_count == 0, (
            f"Run 2 should have 0 query() calls (journal replay), got {fake2.call_count}"
        )
        assert budget2.spent() == 0, (
            f"Run 2 budget.spent() must be 0 (no queries), got {budget2.spent()}"
        )

        # Envelopes match
        assert result2.envelopes.get("t1") == result1.envelopes.get("t1")
        assert result2.envelopes.get("t2") == result1.envelopes.get("t2")
        assert result2.envelopes.get("t3") == result1.envelopes.get("t3")

    def test_upstream_mutation_invalidates_t1_and_t3_but_not_t2(self):
        """Mutating T1's briefing causes T1+T3 to re-dispatch; T2 replays."""
        journal = fresh_journal()

        # Run 1 — populate journal
        budget1 = fresh_budget()
        fake1 = FakeAgent(default_output_tokens=100)
        tasks_original = build_poc_tasks(t1_briefing="original T1 briefing")
        result1 = run(
            run_dag(
                tasks_original, budget=budget1, journal=journal, fake_agent=fake1, est_per_agent=100
            )
        )
        assert fake1.call_count == 3, f"Run 1 should dispatch all 3; got {fake1.call_count}"

        # Capture the original T2 envelope (from run-1, the journal-populating run)
        t2_envelope_original = result1.envelopes.get("t2")
        assert t2_envelope_original is not None, "T2 must have succeeded in run-1"

        # Run 2 — mutate T1's briefing; T2+T3 use identical briefing otherwise
        budget2 = fresh_budget()
        fake2 = FakeAgent(default_output_tokens=100)
        tasks_mutated = build_poc_tasks(t1_briefing="MUTATED T1 briefing — different content")
        result2 = run(
            run_dag(
                tasks_mutated, budget=budget2, journal=journal, fake_agent=fake2, est_per_agent=100
            )
        )

        # T2 must replay from journal (0 extra calls for T2)
        # T1 and T3 must re-dispatch (2 extra calls: t1 + t3)
        # Total calls in run-2 = 2 (T1 + T3 re-dispatch; T2 journal hit)
        assert fake2.call_count == 2, (
            f"Run 2: expected 2 query() calls (T1+T3 re-dispatch, T2 journal hit), "
            f"got {fake2.call_count}.  Calls: {[(c['task_id']) for c in fake2.calls]}"
        )

        dispatched_ids = {c["task_id"] for c in fake2.calls}
        assert "t1" in dispatched_ids, "T1 must re-dispatch after briefing mutation"
        assert "t3" in dispatched_ids, "T3 must re-dispatch (upstream T1 changed)"
        assert "t2" not in dispatched_ids, "T2 must replay from journal (not re-dispatched)"

        # T2 result matches original (journal hit — same envelope as run-1)
        assert result2.envelopes.get("t2") == t2_envelope_original, (
            "T2 journal hit must return the same envelope as run-1"
        )

    def test_journal_key_is_clock_free(self):
        """Journal keys are identical regardless of when they are computed."""
        task = make_task("t1", briefing="same briefing", model="claude-sonnet-4-5")
        journal = ResultJournal()

        key1 = journal.key(task, 1, model="claude-sonnet-4-5", briefing="same briefing")
        # Simulate time passing between the two calls (no actual sleep needed —
        # the key must not incorporate any clock signal)
        key2 = journal.key(task, 1, model="claude-sonnet-4-5", briefing="same briefing")

        assert key1 == key2, (
            f"Journal key must be clock-free: got different keys:\n  key1={key1}\n  key2={key2}"
        )

    def test_different_briefing_produces_different_key(self):
        """Different briefing → different journal key (briefing is part of key)."""
        task = make_task("t1", briefing="briefing A", model="claude-sonnet-4-5")
        journal = ResultJournal()

        key1 = journal.key(task, 1, model="claude-sonnet-4-5", briefing="briefing A")
        key2 = journal.key(task, 1, model="claude-sonnet-4-5", briefing="briefing B")

        assert key1 != key2, "Different briefings must produce different journal keys"


# ── Module-unit coverage (budget.py + journal.py are lifted verbatim to M1) ───


class TestJournalPersistence:
    """journal.py round-trips to disk and survives a corrupt backing file —
    both load-bearing because this module is lifted verbatim into M1."""

    def test_persistence_round_trip(self, tmp_path):
        """Write entries → reconstruct a fresh ResultJournal(path) → lookup matches."""
        jpath = tmp_path / "journal.json"
        task = make_task("t1", briefing="round-trip briefing", model="claude-sonnet-4-5")

        # Journal A — write an entry
        journal_a = ResultJournal(jpath)
        key = journal_a.key(task, 1, model="claude-sonnet-4-5", briefing="round-trip briefing")
        envelope = {
            "type": "task_result",
            "task_id": "t1",
            "attempt": 1,
            "status": "done",
            "artifacts": [{"path": "a.txt", "sha": "abc"}],
        }
        usage = {"input_tokens": 100, "output_tokens": 42}
        journal_a.put(key, envelope, usage=usage)
        assert jpath.exists(), "put() must persist the backing file"

        # Journal B — fresh instance over the same path reads the entry back
        journal_b = ResultJournal(jpath)
        assert journal_b.lookup(key) == envelope, "reconstructed journal must return the entry"
        # envelope_hash survives the round trip
        assert journal_b.get_envelope_hash(key) == journal_a.get_envelope_hash(key)

    def test_corrupt_file_starts_fresh(self, tmp_path):
        """A corrupt/garbage backing file starts fresh without crashing."""
        jpath = tmp_path / "journal.json"
        jpath.write_text("}{ this is not valid json !!!", encoding="utf-8")

        # Construction must not raise; the store starts empty
        journal = ResultJournal(jpath)
        task = make_task("t1", briefing="x", model="claude-sonnet-4-5")
        key = journal.key(task, 1, model="claude-sonnet-4-5", briefing="x")
        assert journal.lookup(key) is None, "corrupt file → empty store, lookup misses"

        # And it is still usable: a put + lookup works
        envelope = {
            "type": "task_result",
            "task_id": "t1",
            "attempt": 1,
            "status": "done",
            "artifacts": [{"path": "a.txt", "sha": "abc"}],
        }
        journal.put(key, envelope, usage={"output_tokens": 10})
        assert journal.lookup(key) == envelope


class TestBudgetPoolUnits:
    """budget.py unit coverage — parent bubbling + the strict-`>` headroom
    boundary, both lifted verbatim into M1."""

    def test_parent_bubbling(self):
        """A child's charge() sums into the parent's spent()."""
        parent = BudgetPool(total_tokens=10_000)
        child = BudgetPool(total_tokens=5_000, parent=parent)

        child.charge({"output_tokens": 120})
        child.charge({"output_tokens": 80})

        assert child.spent() == 200, "child tracks its own spend"
        assert parent.spent() == 200, "child charges bubble into the parent pool"

        # A direct parent charge does NOT leak back into the child
        parent.charge({"output_tokens": 50})
        assert parent.spent() == 250
        assert child.spent() == 200

    def test_headroom_boundary_strict_greater_than(self):
        """spent+est == ceiling is ALLOWED; spent+est > ceiling RAISES.

        Locks the strict-`>` semantics against a future off-by-one (a `>=`
        regression would reject the exact-fit dispatch).
        """
        # total=1000, headroom=0.70 → effective_ceiling = 700
        budget = BudgetPool(total_tokens=1000, headroom=0.70)
        assert budget.effective_ceiling == 700

        # Exactly at the ceiling: spent(0) + est(700) == 700 → ALLOWED
        budget.assert_can_dispatch(700)  # must not raise

        # One token over: spent(0) + est(701) == 701 > 700 → RAISES
        with pytest.raises(BudgetExceeded):
            budget.assert_can_dispatch(701)

        # After charging up to the ceiling, exact-fit of the remainder is allowed
        budget.charge({"output_tokens": 700})
        assert budget.spent() == 700
        budget.assert_can_dispatch(0)  # 700 + 0 == 700 → allowed
        with pytest.raises(BudgetExceeded):
            budget.assert_can_dispatch(1)  # 700 + 1 > 700 → raises


# ── Live CLI marker (deselected by default; runs only with the claude binary) ─


@pytest.mark.live
@pytest.mark.skipif(
    not claude_cli_available(),
    reason="claude CLI binary not on PATH",
)
def test_live_cli_smoke():
    """Smoke the live agent path: confirm the ``claude`` CLI binary is present.

    The live agent path drives ``claude`` directly as a subprocess (subscription
    auth, no API key, no new dependency).  This test is ``@pytest.mark.live`` so
    it is DESELECTED by default (``-q`` runs do not include it) and additionally
    SKIPPED when ``shutil.which("claude")`` finds no binary.  M3 will drive
    ``claude -p --output-format json --json-schema ... --model ...
    --system-prompt ...`` as a subprocess; this smoke is a placeholder that only
    asserts the binary resolves, so the ``-m live`` marker is genuinely wired
    and the README claim is truthful.
    """
    import shutil

    assert claude_cli_available(), "live path requires the claude CLI binary"
    assert shutil.which("claude") is not None, "claude binary must resolve on PATH"
