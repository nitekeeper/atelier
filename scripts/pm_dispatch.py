# scripts/pm_dispatch.py
"""Atelier team-mode PM wave-dispatch loop (atelier#60, AI-3) — MODE-AGNOSTIC.

This module is the wave-5 scheduler core: it partitions a task list into
strict ordered waves, dispatches each wave (bounded concurrency), enforces the
per-task attempt budget + per-attempt wall-clock cap, validates every worker
reply envelope, and gates wave N+1 on wave N reaching a TERMINAL-ONLY closure.

It does NOT spawn workers. Spawning is mode-specific and owned by atelier#61;
this engine reaches the outside world only through three injected seams —
``spawn_fn`` (start a worker for one attempt), ``poll_fn`` (read that worker's
terminal reply envelope, or ``None`` if it has not reported yet), and
``escalate_fn`` (surface an abandonment to PM/human). Both team mode and
sub-agent mode supply their own seam implementations; the wave logic, the
barrier, the budget, and the wall-clock are identical across modes.

Ratified consensus this implements (Phase-3 mesh, atelier#60):

* NULL ``parallel_group`` is rejected fail-loud at PRE-FLIGHT over the whole
  batch — never per-task mid-loop (a skipped task would deadlock the barrier,
  since :class:`~scripts.dispatch.WaveTracker` counts it among ``expected``).
* Wave order is ``(parallel_group ASC, created_at ASC, id ASC)`` — ``id`` is
  the deterministic tiebreaker for same-batch ``created_at`` collisions.
* The barrier gates on ``TERMINAL_ONLY_STATUSES = {done, abandoned}`` (reusing
  :meth:`WaveTracker.terminal_only`); ``blocked`` AND ``needs-input`` both HOLD
  the barrier.
* ``abandoned`` is wave-terminal the instant it is recorded; ``abandoned_ack_at``
  is non-gating audit only.
* The 30-min cap is PM-SIDE: measured from the engine's own dispatch timestamp
  via an INJECTABLE monotonic clock, checked in the poll loop INDEPENDENT of any
  worker signal — so a silently-dead worker (no heartbeat, no envelope) is still
  caught. Heartbeats are informational-only in v1 (no heartbeat-miss kill; the
  kaizen-hardened 60s-emit / 300s-stall design is documented in the rules SKILL
  as the v2 default).

Untrusted content: a reply envelope is untrusted DATA. It is only validated /
pattern-matched / echoed in diagnostics — never executed or interpolated into
anything executable. Validation is delegated to
:func:`scripts.pm_dispatch_envelope.validate_envelope`, which binds identity to
the PM's own dispatch record (anti cross-task spoof + anti attempt-laundering).

────────────────────────────────────────────────────────────────────────────
TERMINATION GUARANTEE (falsifiable proof)
────────────────────────────────────────────────────────────────────────────
Claim: :meth:`WaveDispatcher.run` halts on every finite task list.

(1) Each ATTEMPT halts. A dispatched attempt's poll loop exits when either
    ``poll_fn`` returns an envelope OR ``clock()`` reaches ``t0 + WALL_CLOCK_S``.
    ``clock`` defaults to :func:`time.monotonic` (non-decreasing, strictly
    advancing in real runs); tests inject a clock that advances. So no attempt
    can poll forever — the wall-clock is the hard bound (30 min).
(2) Each TASK halts. ``increment_attempt`` runs exactly once per dispatch, so a
    task's ``attempts`` strictly increases per dispatch. A failed/non-terminal/
    soft-killed attempt re-queues the task ONLY while ``attempts < MAX_ATTEMPTS``;
    on ``attempts >= MAX_ATTEMPTS`` the task is force-abandoned (terminal, no
    re-queue). A ``done``/``abandoned`` envelope is terminal. A cascade-abandon
    is terminal and re-queues nothing. So a task is dispatched at most
    ``MAX_ATTEMPTS`` (5) times, then is terminal.
(3) The whole LOOP halts. There are at most ``len(set(parallel_group))`` waves
    (finite). Within a wave, ``pending`` starts finite and grows only by
    re-queues, each bounded by (2); ``in_flight`` never exceeds
    ``MAX_PARALLEL_WORKERS``. Total dispatches across the run are therefore
    ``<= len(tasks) * MAX_ATTEMPTS`` — finite. ∎

Falsifier: if any code path re-queued a task without an attempt charge, or
charged an attempt without the ``< MAX_ATTEMPTS`` guard before re-queue, (2)
would break and the loop could spin. The single re-queue site is
:meth:`_handle_failed_attempt`, which re-queues only under that guard.

The atelier#78 read-first GO-OBSERVE gate at the deadline trip does NOT add a
re-queue site: the done-but-silent SUCCESS branch routes the confirming-read
envelope through :meth:`_handle_envelope`, whose ``done`` arm calls
``complete_task`` (TERMINAL — NEVER re-queues) and charges NO extra attempt (the
dispatch-time charge stands). The genuinely-stalled branch falls through to the
SAME :meth:`_handle_failed_attempt`. So :meth:`_handle_failed_attempt` remains
the SINGLE re-queue site and the per-task ``<= MAX_ATTEMPTS`` bound is preserved;
the confirming read is one non-blocking poll, not a new wait loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scripts import tasks as tasks_mod
from scripts.caveman_codec import compress as _caveman_compress
from scripts.caveman_codec import should_compress as _caveman_should_compress
from scripts.dispatch import (
    TERMINAL_ONLY_STATUSES,
    DispatchError,
    WaveTracker,
    _valid_positive_int_env,
)
from scripts.pm_dispatch_envelope import (
    ABANDON_RE,
    EnvelopeValidationError,
    validate_envelope,
)

_log = logging.getLogger(__name__)

# ── Caps / constants (consensus item 3 + 4) ────────────────────────────────

#: Max workers dispatched concurrently within a single wave. A wave with more
#: than this many tasks is dispatched in batches of <= this size (the barrier
#: still waits for the WHOLE wave). Mode-agnostic: in sub-agent mode this bounds
#: in-flight background ``Agent`` calls; team mode is bounded the same way.
MAX_PARALLEL_WORKERS = 5

#: The §5.2 5-attempt budget per task. ``attempts`` is incremented once per
#: dispatch; on reaching this ceiling without a terminal-only closure the task
#: is force-abandoned with category ``capacity``.
MAX_ATTEMPTS = 5

#: PM-side per-attempt wall-clock cap, seconds (30 min). Measured from the
#: engine's own dispatch timestamp via :attr:`WaveDispatcher.clock`, NOT from
#: any worker self-report — so a silently-dead worker is still soft-killed.
WALL_CLOCK_S = 1800.0

#: Poll cadence for the in-flight scan when no task made progress this round.
#: Mirrors kaizen's bridge poll interval (polling, not events — SQLite has no
#: notify). Cheap: the scan is in-memory + a non-blocking ``poll_fn`` read.
POLL_INTERVAL_S = 0.2

#: DB statuses that mean a task needs no (further) dispatch. The tasks table
#: stores a successful close as ``complete`` and an abandonment as
#: ``abandoned`` (see migration 006 wave-ordering predicate). Mirrors that
#: predicate so pre-flight and partitioning agree with the durable backend.
_DB_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "abandoned"})

#: Wave-summary context-compression threshold, BYTES. When the accumulated
#: verbatim reply bytes for a just-finished wave exceed this, the verbatim
#: bodies are replaced by a deterministic digest in that wave's summary (the
#: PROACTIVE trigger). 16 KiB is a generous default — small waves never cross
#: it, so default behavior is byte-identical to pre-feature. Tunable per run via
#: ``ATELIER_COMPRESS_THRESHOLD`` (a valid non-negative int, else ignored —
#: reuses :func:`scripts.dispatch._valid_positive_int_env`'s valid-or-ignore
#: posture, so a typo never changes behavior). The accumulator measures only
#: ``notes_md`` + ``repr(artifacts)`` bytes — the verbatim bulk this feature
#: compresses — NOT the small constant-size metadata (type/task_id/attempt/
#: status) the digest always preserves.
COMPRESSION_THRESHOLD_BYTES = 16384

#: Env override for :data:`COMPRESSION_THRESHOLD_BYTES`. A valid non-negative
#: int wins; blank/garbage/negative is ignored (valid-or-ignore).
COMPRESS_THRESHOLD_ENV = "ATELIER_COMPRESS_THRESHOLD"

#: B2 — env gate for the caveman prose codec at the wave-summary digest sink
#: (the genuine model-bound prose path returned to the orchestrator session).
#: DEFAULT OFF: unset/empty/"0"/"false"/"no"/"off" (case-insensitive, trimmed)
#: → disabled; only "1"/"true"/"yes"/"on" enable it. Parsed defensively from
#: the injected ``resolved_env`` (house style A8 — never read ``os.environ``
#: directly), so a typo never flips behavior. When OFF the digest is
#: byte-identical to :func:`default_wave_digest`'s output (the #1 invariant).
CAVEMAN_COMPRESS_ENV = "ATELIER_CAVEMAN_COMPRESS"

#: Truthy markers that enable :data:`CAVEMAN_COMPRESS_ENV` (case-insensitive,
#: trimmed). Anything else — including unset/empty/garbage — is OFF.
_CAVEMAN_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Head/tail slice cap (chars) applied to each retained ``notes_md`` in the
#: deterministic digest. Mirrors :data:`scripts.status._ARTIFACT_PREVIEW_CAP`'s
#: 200-char truncation idiom — a generous-but-bounded preview that makes the
#: verbatim bulk materially smaller without dropping the human-readable lede.
#: NOTE: this caps by CHARACTER count, whereas the proactive TRIGGER accumulates
#: by UTF-8 BYTES (``_wave_reply_bytes``). The two units differ for multibyte
#: content, which is fine: the trigger is a conservative byte-budget gate and the
#: cap is a per-notes preview bound — the digest stays materially smaller either
#: way (asserted by ``test_default_wave_digest_materially_smaller``).
_DIGEST_NOTES_CAP = 200


# ── Exceptions ──────────────────────────────────────────────────────────────


class NullParallelGroupError(DispatchError):
    """Raised by :func:`preflight_validate` when one or more non-terminal tasks
    carry a NULL ``parallel_group``.

    Carries the sorted list of offending ``task_ids`` so the operator can fix
    the planner output at source — mirroring
    :class:`scripts.dispatch.MissingRenderVarsError`'s sorted-names diagnostic.
    The wave model REQUIRES a non-null group on every dispatchable task; a NULL
    has no defined wave, and silently bucketing it (wave 0, or wave ∞) would
    risk running a dependent task in the wrong wave — the exact race waves
    exist to prevent. So we reject the WHOLE batch before any dispatch.
    """

    def __init__(self, task_ids: Iterable[Any]) -> None:
        self.task_ids: list[Any] = sorted(task_ids, key=lambda t: str(t))
        super().__init__(
            f"{len(self.task_ids)} non-terminal task(s) have NULL parallel_group "
            f"(no defined wave): {self.task_ids}. The planner MUST assign a "
            "parallel_group to every dispatchable task; zero tasks were "
            "dispatched."
        )


# ── Pure helpers (no IO; unit-testable in isolation — AI-5) ──────────────────


def _task_id(task: Mapping[str, Any]) -> Any:
    """The task's primary key. Required field; KeyError if a caller passes a
    malformed task dict (fail loud — a task with no id cannot be tracked)."""
    return task["id"]


def _is_db_terminal(task: Mapping[str, Any]) -> bool:
    """True iff the task's persisted status means it needs no dispatch."""
    return task.get("status") in _DB_TERMINAL_STATUSES


def _depends_on(task: Mapping[str, Any]) -> list[Any]:
    """Upstream task ids this task depends on.

    ``depends_on`` is validation-time metadata: the planner does NOT persist it
    (only ``parallel_group`` is durable — see ``scripts/planner.py``). The
    orchestrator threads the in-memory task dicts (which still carry the edges)
    into :meth:`WaveDispatcher.run`, so cascade-abandon reads them from the dict
    rather than the DB. Absent/None → no dependencies.
    """
    deps = task.get("depends_on")
    return list(deps) if deps else []


def preflight_validate(tasks: Sequence[Mapping[str, Any]]) -> None:
    """ATOMIC whole-batch NULL-``parallel_group`` gate (consensus item 1).

    Run ONCE at the very top of :meth:`WaveDispatcher.run`, BEFORE any wave is
    dispatched. Every non-terminal task MUST carry a non-null ``parallel_group``;
    if ANY does not, raise :class:`NullParallelGroupError` naming ALL offenders
    and dispatch NOTHING. A per-task mid-loop skip is deliberately NOT done: the
    skipped task would still be in ``WaveTracker.expected`` and the barrier
    (:meth:`WaveTracker.terminal_only`) would never satisfy → deadlock.
    """
    offenders = [
        _task_id(t) for t in tasks if not _is_db_terminal(t) and t.get("parallel_group") is None
    ]
    if offenders:
        raise NullParallelGroupError(offenders)


def partition_waves(
    tasks: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Partition ``tasks`` into ordered waves (consensus item 2).

    Sort by ``(parallel_group ASC, created_at ASC, id ASC)`` — ``id`` is the
    deterministic tiebreaker so same-batch ``created_at`` collisions still yield
    a total, reproducible order (matters for stable logs/replay/tests). A wave
    is a maximal run of equal ``parallel_group``. Already-terminal tasks are
    excluded (no point dispatching a ``complete``/``abandoned`` row).

    Pre-flight (:func:`preflight_validate`) must have run first, so every
    remaining task here has a non-null ``parallel_group``.
    """
    live = [t for t in tasks if not _is_db_terminal(t)]
    ordered = sorted(
        live,
        key=lambda t: (t["parallel_group"], t.get("created_at") or "", _task_id(t)),
    )
    waves: list[list[Mapping[str, Any]]] = []
    current_group: Any = object()  # sentinel that equals nothing
    for task in ordered:
        group = task["parallel_group"]
        if group != current_group:
            waves.append([])
            current_group = group
        waves[-1].append(task)
    return waves


def wave_gate_satisfied(tracker: WaveTracker) -> bool:
    """The wave barrier predicate (consensus items 3 + 4).

    Wave N+1 may start ONLY when this returns True for wave N. It wraps the
    REUSED :meth:`WaveTracker.terminal_only` — True iff every expected task has
    reported a TERMINAL-ONLY status (``done``/``abandoned``). ``blocked`` and
    ``needs-input`` are non-terminal and HOLD the barrier.

    NOTE: ``abandoned_ack_at`` is intentionally NOT consulted here (consensus
    item 5) — an abandoned task is wave-terminal the moment it is recorded; the
    PM/human acknowledgement is non-gating audit, never a barrier.
    """
    return tracker.terminal_only()


#: Alias — the mesh referred to this predicate by both names.
wave_can_advance = wave_gate_satisfied


def _parse_abandon_category(notes_md: str) -> str:
    """Extract the TM-006 abandon category from ``notes_md`` line 1.

    :func:`validate_envelope` already guaranteed line 1 matches
    :data:`~scripts.pm_dispatch_envelope.ABANDON_RE` before we get here, so the
    match cannot be None on a validated ``abandoned`` envelope. Defensive
    fallback to ``capacity`` keeps the engine total if a caller bypasses
    validation: ``capacity`` is a documented TM-006 grammar token (the
    engine-forced default used on budget exhaustion), whereas ``other`` is NOT a
    valid category — emitting it would write an out-of-grammar value into
    ``tasks.abandon_category``. This branch is unreachable on a validated
    envelope; it only fires on a contract bypass.
    """
    first_line = notes_md.splitlines()[0] if notes_md else ""
    match = ABANDON_RE.match(first_line)
    return match.group("category") if match else "capacity"


# ── Wave-summary context compression (deterministic, no-LLM default) ─────────


def _digest_notes(notes_md: str, status: str) -> str:
    """Compress ``notes_md`` to a bounded head+tail preview with a truncation
    marker (mirrors :func:`scripts.status._truncate`'s ``…(+N more)`` idiom).

    INVARIANT for ``status == "abandoned"``: line 1 is preserved VERBATIM and
    is NEVER cut, because :func:`_parse_abandon_category` re-parses it against
    the single-sourced :data:`~scripts.pm_dispatch_envelope.ABANDON_RE` grammar
    downstream. The head/tail compression applies only to the bulk BELOW line 1
    on an abandoned envelope; on any other status the whole body is compressed.

    Pure + deterministic: same input → same output, no clock, no IO.
    """
    if status == "abandoned":
        lines = notes_md.splitlines()
        first_line = lines[0] if lines else ""
        rest = "\n".join(lines[1:])
        if not rest:
            return first_line
        return f"{first_line}\n{_head_tail(rest, _DIGEST_NOTES_CAP)}"
    return _head_tail(notes_md, _DIGEST_NOTES_CAP)


def _head_tail(text: str, cap: int) -> str:
    """Keep the first ``cap`` chars + the last ``cap`` chars of ``text`` with an
    explicit ``…(+N more)…`` marker naming the elided byte-count. When the text
    already fits in ``2 * cap`` it is returned unchanged (no marker — nothing was
    cut). Pure; mirrors status.py's truncation-is-explicit posture."""
    if len(text) <= cap * 2:
        return text
    elided = len(text) - cap * 2
    return f"{text[:cap]}…(+{elided} more)…{text[-cap:]}"


def _digest_artifacts(artifacts: Any) -> str:
    """Summarize an envelope's ``artifacts`` as ``count + paths`` rather than the
    verbatim entries. Untrusted DATA — entries are only stringified, never
    executed (mirrors :func:`scripts.status._render_artifact`). Pure."""
    if not isinstance(artifacts, list):
        return "artifacts=0"
    paths: list[str] = []
    for entry in artifacts:
        if isinstance(entry, Mapping) and entry.get("path") is not None:
            paths.append(str(entry.get("path")))
        else:
            paths.append(repr(entry))
    return f"artifacts={len(artifacts)} [{', '.join(paths)}]"


def default_wave_digest(envelopes: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic, pure, NO-LLM digest of a wave's retained reply envelopes.

    This is the production fallback for the injected ``summarize_fn`` seam: it
    replaces the verbatim bulk bodies that piled up across a wave with a
    byte-budgeted summary, while PRESERVING every field the downstream judge /
    abandonment path needs — ``task_id``, ``attempt``, ``status`` are kept
    verbatim, and for an ``abandoned`` envelope line 1 of ``notes_md`` is kept
    verbatim (it must still match :data:`~scripts.pm_dispatch_envelope.ABANDON_RE`
    when re-parsed). ``notes_md`` bulk is head/tail-sliced and ``artifacts`` are
    collapsed to count + paths.

    Deterministic + pure: same envelope list → byte-identical digest string, no
    clock, no IO, no tokenizer dependency. Materially smaller than the verbatim
    input on any wave large enough to cross the byte threshold.
    """
    blocks: list[str] = []
    for env in envelopes:
        task_id = env.get("task_id")
        attempt = env.get("attempt")
        status = env.get("status")
        notes_md = env.get("notes_md")
        notes = _digest_notes(notes_md if isinstance(notes_md, str) else "", str(status))
        arts = _digest_artifacts(env.get("artifacts"))
        blocks.append(
            f"task_id={task_id!r} attempt={attempt!r} status={status!r}\n  {arts}\n  notes: {notes}"
        )
    return "\n".join(blocks)


def compress_reply_for_context(text: str, *, enabled: bool, level: str = "full") -> str:
    """B2 sink — caveman-compress a model-bound prose ``text`` IFF ``enabled``.

    This is the env-gated codec wrapper applied to the wave-summary ``digest``
    string (the genuine model-bound prose returned to the orchestrator session).
    It is NEVER applied to a stored/parsed envelope — only to the SEPARATE digest
    string — so byte-sensitive parsers (``validate_envelope`` /
    ``_parse_abandon_category`` / ``ABANDON_RE``) never see codec-mutated bytes.

    Contract (returns ``text`` UNCHANGED — the #1 OFF byte-identity invariant —
    in any of these cases):
      - ``enabled`` is False (default; gate OFF),
      - ``text`` is not a ``str`` or is empty,
      - :func:`caveman_codec.should_compress` is False (auto-clarity gate:
        security / destructive / multi-step content passes through verbatim).
    Otherwise returns :func:`caveman_codec.compress(text, level)`.
    """
    if not enabled:
        return text
    if not isinstance(text, str) or text == "":
        return text
    if not _caveman_should_compress(text):
        return text
    return _caveman_compress(text, level)


# ── In-flight bookkeeping ────────────────────────────────────────────────────


@dataclass
class _InFlight:
    """One dispatched attempt awaiting a reply. ``t0`` is the engine-side
    dispatch instant (from the injected clock) — the wall-clock is measured
    against THIS, not any worker signal."""

    task: Mapping[str, Any]
    t0: float
    attempt: int


# ── Escalation record (PM → human surface; consensus item 8) ─────────────────


def _default_escalate(escalation: Mapping[str, Any]) -> None:
    """Default escalation sink: log a WARNING. The orchestrator (#61) injects a
    real surface (inline milestone message to the user). This is never silent —
    item 8 requires escalation be GUARANTEED-EMITTED, so the default still
    emits."""
    _log.warning(
        "ABANDON ESCALATION task_id=%s worker=%s attempt=%s category=%s last_status=%r upstream=%s",
        escalation.get("task_id"),
        escalation.get("worker"),
        escalation.get("attempt"),
        escalation.get("category"),
        escalation.get("last_status"),
        escalation.get("upstream_task_id"),
    )


# ── The engine ───────────────────────────────────────────────────────────────


class WaveDispatcher:
    """Mode-agnostic wave-dispatch loop.

    The three outward seams are injected so the engine carries zero
    mode-specific knowledge (atelier#61 owns spawning):

    * ``spawn_fn(task, attempt) -> None`` — start a worker for one attempt.
      Fire-and-forget; the engine times it via the wall-clock and reads the
      result through ``poll_fn``.
    * ``poll_fn(task, attempt) -> Mapping | None`` — return the worker's
      terminal reply envelope if it has reported, else ``None``. MUST be
      non-blocking (a DB/bridge read), since the engine polls it.
    * ``escalate_fn(escalation) -> None`` — surface an abandonment to PM/human.

    Plus two test seams: ``clock`` (default :func:`time.monotonic`; injectable —
    atelier bans argless ``datetime.now()``/``Date.now()``) and ``sleep_fn``
    (default :func:`time.sleep`).
    """

    def __init__(
        self,
        db_path: str,
        *,
        spawn_fn: Callable[[Mapping[str, Any], int], None] | None = None,
        poll_fn: Callable[[Mapping[str, Any], int], Mapping[str, Any] | None] | None = None,
        escalate_fn: Callable[[Mapping[str, Any]], None] = _default_escalate,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        summarize_fn: Callable[[list[Mapping[str, Any]]], str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.db_path = db_path
        self._spawn_fn = spawn_fn if spawn_fn is not None else self._unset_spawn
        self._poll_fn = poll_fn if poll_fn is not None else self._unset_poll
        self._escalate_fn = escalate_fn
        self.clock = clock
        self.sleep_fn = sleep_fn
        #: Wave-summary compression seam (atelier wave-summary context
        #: compression). ``summarize_fn(retained_envelopes) -> str`` produces the
        #: digest emitted into a wave summary when the per-wave verbatim reply
        #: bytes cross the threshold. ``None`` → the deterministic, no-LLM
        #: :func:`default_wave_digest` (the feature is FUNCTIONAL in production
        #: with no LLM dependency; a real Haiku summarizer can be injected later
        #: through this SAME seam). The engine never spawns synchronously — this
        #: seam is the only LLM door and it is opt-in.
        self._summarize_fn = summarize_fn if summarize_fn is not None else default_wave_digest
        #: Proactive compression threshold (bytes). Env ``ATELIER_COMPRESS_THRESHOLD``
        #: (valid non-negative int) overrides the module default; garbage/blank/
        #: negative is ignored (valid-or-ignore, reusing dispatch's helper).
        resolved_env = env if env is not None else None
        self._compress_threshold = (
            _valid_positive_int_env(
                resolved_env.get(COMPRESS_THRESHOLD_ENV), COMPRESSION_THRESHOLD_BYTES
            )
            if resolved_env is not None
            else COMPRESSION_THRESHOLD_BYTES
        )
        #: B2 — caveman prose codec gate at the wave-summary digest sink. Parsed
        #: from the injected env (NEVER os.environ; house style A8). DEFAULT OFF:
        #: only an explicit truthy marker ("1"/"true"/"yes"/"on", case-insensitive,
        #: trimmed) enables it; unset/empty/"0"/"false"/"no"/"off"/garbage → OFF,
        #: so the digest is byte-identical to default_wave_digest's output.
        self._caveman_enabled: bool = (
            (resolved_env.get(CAVEMAN_COMPRESS_ENV) or "").strip().lower() in _CAVEMAN_TRUTHY
            if resolved_env is not None
            else False
        )
        #: Every escalation emitted this run, in order (audit; item 8).
        self.escalations: list[dict[str, Any]] = []
        #: Task ids that ended ``abandoned`` (worker / budget / cascade) — the
        #: cascade-abandon source set, accumulated across waves.
        self.abandoned_ids: set[Any] = set()
        self._task_index: dict[Any, Mapping[str, Any]] = {}
        #: Per-wave RETAINED validated envelope bodies (reset at each wave
        #: boundary). ``_handle_envelope`` appends each validated envelope here as
        #: it passes; the wave boundary digests them if the byte budget is
        #: crossed. Today the engine DROPPED these (WaveTracker.reports keeps only
        #: the status string), so replies accumulated only in the orchestrator's
        #: CC context — this retains them in Python for the digest.
        self._wave_envelopes: list[Mapping[str, Any]] = []
        #: Accumulated verbatim reply bytes for the current wave
        #: (``len(notes_md utf-8) + len(repr(artifacts))``), reset per wave.
        self._wave_reply_bytes = 0

    # ── seam defaults (mode-agnostic engine has no spawner of its own) ──

    @staticmethod
    def _unset_spawn(task: Mapping[str, Any], attempt: int) -> None:
        raise NotImplementedError(
            "WaveDispatcher.spawn_fn is not set: spawning is mode-specific "
            "(atelier#61). Inject spawn_fn to run the engine."
        )

    @staticmethod
    def _unset_poll(task: Mapping[str, Any], attempt: int) -> Mapping[str, Any] | None:
        raise NotImplementedError(
            "WaveDispatcher.poll_fn is not set: reply collection is "
            "mode-specific (atelier#61). Inject poll_fn to run the engine."
        )

    # ── public entry ────────────────────────────────────────────────────

    def run(self, tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Dispatch ``tasks`` wave by wave; return a per-wave summary list.

        Strict barrier: wave N+1 is not dispatched until wave N's
        :func:`wave_gate_satisfied` holds. Returns one
        :meth:`WaveTracker.summary` dict per wave (in order).
        """
        preflight_validate(tasks)  # item 1 — atomic, before ANY dispatch
        self._task_index = {_task_id(t): t for t in tasks}
        waves = partition_waves(tasks)

        summaries: list[dict[str, Any]] = []
        for index, wave in enumerate(waves):
            # Reset the per-wave reply accumulators BEFORE dispatching this wave
            # (the digest is per-wave; bytes/bodies never leak across the
            # boundary).
            self._wave_envelopes = []
            self._wave_reply_bytes = 0
            tracker = WaveTracker(
                wave_id=f"wave-{index}",
                expected={str(_task_id(t)) for t in wave},
            )
            # item 9 — cascade-abandon dependents of prior-wave abandons at THIS
            # wave's pre-flight, BEFORE dispatching anything in the wave.
            runnable = self._cascade_preflight(wave, tracker)
            self._dispatch_wave(runnable, tracker)
            # Barrier: by construction every wave task is recorded done/abandoned,
            # so the gate is satisfied here. Assert it to make the invariant loud
            # if a future edit breaks it.
            if not wave_gate_satisfied(tracker):  # pragma: no cover — invariant
                raise DispatchError(
                    f"wave {index} gate not satisfied after dispatch "
                    f"(outstanding={sorted(tracker.outstanding())}); refusing to "
                    "advance — this is a scheduler bug, not a worker outcome."
                )
            summary = tracker.summary()
            # Wave-summary context compression (PROACTIVE trigger): if this
            # just-finished wave's accumulated verbatim reply bytes crossed the
            # threshold, replace the verbatim bulk with a digest in the wave
            # summary. Byte-IDENTICAL to pre-feature when the threshold is not
            # crossed — no `compressed` key, no digest, summary untouched.
            self._maybe_compress_wave(summary)
            summaries.append(summary)
        return summaries

    # ── wave-level steps ────────────────────────────────────────────────

    def _cascade_preflight(
        self, wave: Sequence[Mapping[str, Any]], tracker: WaveTracker
    ) -> list[Mapping[str, Any]]:
        """Cascade-abandon any wave task that (transitively) depends on an
        already-abandoned task (consensus item 9).

        Such a task can never get correct upstream output, so dispatching it is
        pointless. Each cascade: ``set_abandoned(category='blocked')`` naming the
        upstream id, a GUARANTEED escalation, and NO attempt charge. A bounded
        visited-set walk over the in-memory dependency graph survives a cyclic
        ``depends_on`` (malformed planner output) without looping.
        """
        runnable: list[Mapping[str, Any]] = []
        for task in wave:
            upstream = self._first_abandoned_ancestor(task)
            if upstream is not None:
                self._abandon_and_escalate(
                    task,
                    category="blocked",
                    attempt=int(task.get("attempts") or 0),
                    last_status="cascade",
                    upstream_task_id=upstream,
                    charge_attempt=False,  # item 9 — cascade does NOT spend budget
                    # atelier#78 — carry the wave's surviving terminal state on
                    # the cascade abandon too (mirrors the capacity-abandon path).
                    surviving_state=self._snapshot_surviving(tracker),
                )
                tracker.record(str(_task_id(task)), "abandoned")
                self.abandoned_ids.add(_task_id(task))
            else:
                runnable.append(task)
        return runnable

    def _first_abandoned_ancestor(self, task: Mapping[str, Any]) -> Any | None:
        """Return the first abandoned task id reachable via ``depends_on``, or
        None. Bounded BFS with a visited-set → terminates on cyclic graphs."""
        visited: set[Any] = set()
        frontier: list[Any] = list(_depends_on(task))
        while frontier:
            dep_id = frontier.pop()
            if dep_id in visited:
                continue
            visited.add(dep_id)
            if dep_id in self.abandoned_ids:
                return dep_id
            dep_task = self._task_index.get(dep_id)
            if dep_task is not None:
                frontier.extend(_depends_on(dep_task))
        return None

    def _dispatch_wave(self, runnable: Sequence[Mapping[str, Any]], tracker: WaveTracker) -> None:
        """Dispatch ``runnable`` with <= ``MAX_PARALLEL_WORKERS`` in flight,
        polling each attempt and enforcing the PM-side wall-clock, until every
        task in the wave is recorded terminal-only."""
        pending: list[Mapping[str, Any]] = list(runnable)
        in_flight: dict[Any, _InFlight] = {}

        while pending or in_flight:
            # Top up the in-flight set from pending (batch to the cap; item 4).
            while pending and len(in_flight) < MAX_PARALLEL_WORKERS:
                task = pending.pop(0)
                attempt = self._charge_dispatch(task)
                self._spawn_fn(task, attempt)
                in_flight[_task_id(task)] = _InFlight(task, self.clock(), attempt)

            progressed = False
            for tid, infl in list(in_flight.items()):
                envelope = self._poll_fn(infl.task, infl.attempt)
                now = self.clock()
                if envelope is not None:
                    del in_flight[tid]
                    self._handle_envelope(infl, envelope, tracker, pending)
                    progressed = True
                elif now - infl.t0 >= WALL_CLOCK_S:
                    # item 5 + atelier#78 — PM-side wall-clock READ-FIRST GO-OBSERVE.
                    # A fired deadline is a GO-OBSERVE trigger, NOT an auto-kill
                    # (kaizen F15 ported into the engine). Before charging the
                    # soft-kill the engine performs ONE mandatory confirming
                    # poll_fn re-read of the worker's terminal reply: a
                    # done-but-silent worker (its terminal envelope landed on the
                    # bridge between the last in-flight scan and the deadline) is
                    # captured as a SUCCESS via _handle_envelope — re-validated
                    # against the dispatched (task_id, attempt) before any status
                    # routing, then complete_task with NO extra charge (the
                    # dispatch-time charge stands as the successful attempt). Only
                    # a genuinely-stalled worker (the confirming read still returns
                    # no validated terminal envelope) is soft-killed and charged.
                    # The decision keys on a REAL signal (presence of a validated
                    # terminal envelope), never on elapsed time alone. The
                    # confirming read is non-consuming (production build_poll_fn
                    # passes update_cursor=False), so it cannot hide the row from
                    # the real consumer or be attempt-laundered.
                    del in_flight[tid]
                    final = self._observe_before_kill(infl)
                    if final is not None:
                        # DONE-BUT-SILENT — terminal success via _handle_envelope
                        # (validate_envelope anti-spoof → complete_task,
                        # charge_attempt=False, NO extra increment).
                        self._handle_envelope(infl, final, tracker, pending)
                    else:
                        # GENUINELY STALLED — the already-charged dispatch stands
                        # as the attempt (migration 006: "incremented once per
                        # dispatch; a wall-clock soft-kill counts as an attempt").
                        self._handle_failed_attempt(
                            infl,
                            "soft-kill: wall-clock 30min exceeded - final read empty/non-terminal",
                            tracker,
                            pending,
                        )
                    progressed = True

            if in_flight and not progressed:
                self.sleep_fn(POLL_INTERVAL_S)

    # ── read-first GO-OBSERVE (atelier#78) ───────────────────────────────

    def _observe_before_kill(self, infl: _InFlight) -> Mapping[str, Any] | None:
        """Confirming re-read at the deadline trip: re-invoke the SAME injected
        ``poll_fn`` ONCE for this in-flight attempt and return its result.

        This is the engine-side GO-OBSERVE gate (kaizen F15): a fired wall-clock
        is a trigger to OBSERVE the worker's final state, not an auto-kill. The
        production ``poll_fn`` (:func:`scripts.dispatch.build_poll_fn`) reads the
        bridge with ``update_cursor=False`` (idempotent — consumes nothing, so a
        real consumer can still see the row) and binds ``validate_envelope`` to
        the dispatched ``(task_id, attempt)`` (anti-spoof / anti-attempt-laundering),
        returning a TERMINAL-ONLY envelope or ``None``. So this re-read is a
        side-effect-free real-signal probe: a non-``None`` result means the
        worker's terminal reply landed between the last in-flight scan and the
        deadline (done-but-silent); ``None`` means genuinely stalled.

        Fail-closed: any exception is swallowed to ``None`` (mirrors poll_fn's own
        except at ``dispatch.py``) — a read error is "no terminal reply", which
        HOLDS the GO-OBSERVE gate closed and charges the soft-kill, never silently
        advancing on a read failure."""
        try:
            return self._poll_fn(infl.task, infl.attempt)
        except Exception:
            return None

    # ── per-attempt outcome handling ─────────────────────────────────────

    def _charge_dispatch(self, task: Mapping[str, Any]) -> int:
        """Charge one attempt against the budget and stamp the dispatch time.

        Exactly ONE ``increment_attempt`` per dispatch (migration 006 / item 5):
        the attempt is spent when dispatched, regardless of outcome — so a
        soft-killed dispatch has already counted, and we never double-charge.
        Returns the new ``attempts`` value (the dispatched attempt number, fed
        to ``validate_envelope`` as ``dispatched_attempt``)."""
        row = tasks_mod.increment_attempt(self.db_path, _task_id(task))
        tasks_mod.stamp_last_attempt(self.db_path, _task_id(task))
        return int(row["attempts"])

    def _handle_envelope(
        self,
        infl: _InFlight,
        envelope: Mapping[str, Any],
        tracker: WaveTracker,
        pending: list[Mapping[str, Any]],
    ) -> None:
        """Validate a returned envelope and route by status (items 6 + 8)."""
        try:
            validated = validate_envelope(
                envelope,
                dispatched_task_id=_task_id(infl.task),
                dispatched_attempt=infl.attempt,
            )
        except EnvelopeValidationError as exc:
            # item 6 — a validation failure is a FAILED attempt; NEVER coerce a
            # malformed envelope into a done/abandoned closure.
            self._handle_failed_attempt(infl, f"invalid envelope: {exc}", tracker, pending)
            return

        # Wave-summary context compression — RETAIN the validated envelope body
        # for this wave + accumulate its verbatim reply bytes. (Today the engine
        # drops the body; only the status string survives in WaveTracker.reports.
        # Retaining here lets the wave boundary digest the bulk instead of letting
        # it pile up unboundedly in the orchestrator's CC context.) Capture is
        # pure bookkeeping — it routes NOTHING and changes NO status decision.
        self._retain_envelope(validated)

        status = validated["status"]
        if status in TERMINAL_ONLY_STATUSES:
            if status == "abandoned":
                # Worker self-abandoned (its own budget/cap). Record durably with
                # the parsed category + a GUARANTEED escalation. Attempt already
                # charged at dispatch → charge_attempt=False.
                category = _parse_abandon_category(validated.get("notes_md") or "")
                self._abandon_and_escalate(
                    infl.task,
                    category=category,
                    attempt=infl.attempt,
                    last_status="abandoned",
                    charge_attempt=False,
                )
                self.abandoned_ids.add(_task_id(infl.task))
            elif status == "failed":
                # Hard run-and-failed signal (bounded + hardened path): the worker
                # RAN and hit a deterministic hard failure — distinct from the
                # RETRYABLE blocked/needs-input. It is TERMINAL: record + escalate
                # but DO NOT re-dispatch (a hard failure is not worth burning the
                # remaining MAX_ATTEMPTS retries on). We route through the SAME
                # _abandon_and_escalate path as abandoned (durable set_abandoned +
                # guaranteed escalation) with category 'failed' so the wave closes
                # deterministically. Attempt already charged at dispatch →
                # charge_attempt=False. This adds NO new re-queue site (it only
                # CLOSES), so the termination proof's single re-queue site
                # (_handle_failed_attempt) is preserved.
                self._abandon_and_escalate(
                    infl.task,
                    category="failed",
                    attempt=infl.attempt,
                    last_status="failed",
                    charge_attempt=False,
                )
                self.abandoned_ids.add(_task_id(infl.task))
            else:
                # status == "done" — persist the success terminal state durably,
                # symmetric with the abandon path above. Without this the `done`
                # outcome lived only in the in-memory tracker, so a crash/resume
                # would re-dispatch an already-completed task (asymmetric with
                # `abandoned`, which flips the DB status). `complete_task` flips
                # `tasks.status` -> 'complete' (a _DB_TERMINAL_STATUSES member),
                # making the close resume-idempotent: partition_waves excludes it
                # on the next run. Mode-agnostic: complete_task routes through the
                # backend facade's update_task_status (works in both Local and
                # Memex mode), so this call carries no mode-specific knowledge —
                # the same contract that lets the abandon path stay engine-neutral.
                tasks_mod.complete_task(self.db_path, _task_id(infl.task))
            tracker.record(str(_task_id(infl.task)), status)  # done | abandoned | failed
        else:
            # blocked | needs-input — non-terminal, HOLDS the barrier. In v1
            # (no inline answer mechanism in the engine) an unanswered
            # blocked/needs-input is treated as a failed attempt: re-dispatch
            # until the budget is spent, then abandon. Keeps termination bounded.
            self._handle_failed_attempt(infl, f"non-terminal status {status!r}", tracker, pending)

    # ── wave-summary context compression ─────────────────────────────────

    def _retain_envelope(self, validated: Mapping[str, Any]) -> None:
        """Retain a validated envelope for this wave + accumulate its verbatim
        reply byte-size. Per the design, the accumulator measures only the
        verbatim BULK this feature compresses — ``notes_md`` (utf-8) +
        ``repr(artifacts)`` — never the small constant-size metadata. Pure
        bookkeeping: appends to per-wave state, no routing, no IO."""
        self._wave_envelopes.append(validated)
        notes_md = validated.get("notes_md")
        notes_bytes = len(notes_md.encode("utf-8")) if isinstance(notes_md, str) else 0
        self._wave_reply_bytes += notes_bytes + len(repr(validated.get("artifacts")))

    def _maybe_compress_wave(self, summary: dict[str, Any]) -> None:
        """At the wave boundary, replace the verbatim reply bulk with a digest
        IFF this wave's accumulated reply bytes crossed the threshold.

        No-op (byte-identical to pre-feature) when the threshold is not crossed:
        the summary is left untouched, no ``compressed`` / ``digest`` keys are
        added. When crossed, ``summarize_fn`` (default
        :func:`default_wave_digest`) is invoked ONCE for the wave over the
        retained envelopes, and the digest is stored on the summary under
        ``digest`` with a ``compressed`` flag + the pre-compression byte-count —
        the verbatim bodies are NOT carried in the summary (they never were:
        ``WaveTracker.summary`` only ever held status strings, so the metadata
        the judge/abandonment path needs — status/task_id/attempt + abandon
        line-1 — is preserved inside the digest, not the dropped bulk)."""
        if self._wave_reply_bytes <= self._compress_threshold:
            return
        digest = self._summarize_fn(list(self._wave_envelopes))
        # B2 — env-gated caveman codec on the model-bound digest string ONLY.
        # `default_wave_digest` (the injected `summarize_fn`) stays PURE — the
        # codec runs HERE, never inside it. When the gate is OFF (the default)
        # this is a no-op and the digest is byte-identical to `summarize_fn`'s
        # output. The codec only ever touches THIS digest string, never the
        # retained/validated envelopes in `self._wave_envelopes`.
        digest = compress_reply_for_context(digest, enabled=self._caveman_enabled)
        summary["compressed"] = True
        summary["reply_bytes"] = self._wave_reply_bytes
        summary["digest"] = digest

    def _handle_failed_attempt(
        self,
        infl: _InFlight,
        reason: str,
        tracker: WaveTracker,
        pending: list[Mapping[str, Any]],
    ) -> None:
        """A dispatch did not close the task (invalid / non-terminal / soft-kill).

        The attempt was already charged at dispatch. If the budget is spent
        (``attempt >= MAX_ATTEMPTS``) force-abandon with ``capacity`` + escalate
        (item 7). Otherwise re-queue for another attempt — the SINGLE re-queue
        site (see the termination proof); the ``< MAX_ATTEMPTS`` guard is what
        bounds the loop.
        """
        if infl.attempt >= MAX_ATTEMPTS:
            self._abandon_and_escalate(
                infl.task,
                category="capacity",
                attempt=infl.attempt,
                last_status=reason,
                charge_attempt=False,  # already charged at dispatch
                # atelier#78 — snapshot what survived BEFORE recording this task
                # abandoned, so the escalation carries the wave's partial progress.
                surviving_state=self._snapshot_surviving(tracker),
            )
            tracker.record(str(_task_id(infl.task)), "abandoned")
            self.abandoned_ids.add(_task_id(infl.task))
        else:
            _log.info(
                "task_id=%s attempt=%s did not close (%s); re-dispatching (budget %s/%s)",
                _task_id(infl.task),
                infl.attempt,
                reason,
                infl.attempt,
                MAX_ATTEMPTS,
            )
            pending.append(infl.task)

    def _snapshot_surviving(self, tracker: WaveTracker) -> dict[str, Any]:
        """Snapshot the wave's surviving terminal state for an abandon escalation
        (atelier#78 — mirrors the meeting backstop's PARTIAL flag).

        On a force-abandon the escalation MUST carry what SURVIVED, never a bare
        reason string: which sibling tasks already reached a terminal-only
        closure (``terminal_tasks``), which are still outstanding
        (``outstanding_tasks``), and the wave id — so the operator/PM sees the
        partial progress instead of a context-free termination. Sourced ONLY from
        the in-memory :class:`WaveTracker` (``summary()`` / ``outstanding()``) —
        NO new mutator, NO durable write (A2 facade intact; preserves Memex-mode
        parity since the snapshot introduces no backend call)."""
        summary = tracker.summary()
        reports = summary.get("reports") or {}
        terminal_tasks = sorted(
            role_id for role_id, status in reports.items() if status in TERMINAL_ONLY_STATUSES
        )
        # `artifacts` is reserved for last-validated-envelope pointers; the
        # in-memory tracker does not retain envelope bodies, so v1 emits [] (no
        # new mutator to harvest them). The wave_id + terminal/outstanding split
        # is the surviving-state signal operators consume.
        return {
            "wave_id": summary.get("wave_id"),
            "terminal_tasks": terminal_tasks,
            "outstanding_tasks": sorted(tracker.outstanding()),
            "artifacts": [],
        }

    def _abandon_and_escalate(
        self,
        task: Mapping[str, Any],
        *,
        category: str,
        attempt: int,
        last_status: str,
        charge_attempt: bool,
        upstream_task_id: Any | None = None,
        surviving_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an abandonment AND emit its escalation on the SAME code path
        (consensus item 8 — escalation is GUARANTEED, never best-effort).

        ``set_abandoned`` (durable) runs first so the task is wave-terminal; then
        the escalation is appended + handed to ``escalate_fn`` unconditionally.
        ``charge_attempt`` is only True for paths that must spend budget here
        (none today — dispatch already charges; cascade item 9 never charges).

        ``surviving_state`` (atelier#78) is the in-memory :meth:`_snapshot_surviving`
        snapshot — when present it is attached to the escalation BEFORE
        ``escalate_fn`` so the abandon carries what survived, never a bare reason.
        """
        if charge_attempt:
            tasks_mod.increment_attempt(self.db_path, _task_id(task))
        tasks_mod.set_abandoned(self.db_path, _task_id(task), category)

        escalation: dict[str, Any] = {
            "kind": "escalation",
            "task_id": _task_id(task),
            "worker": task.get("assigned_to"),
            "attempt": attempt,
            "category": category,
            "last_status": last_status,
            "upstream_task_id": upstream_task_id,
        }
        if surviving_state is not None:
            escalation["surviving_state"] = dict(surviving_state)
        self.escalations.append(escalation)
        self._escalate_fn(escalation)  # GUARANTEED — same path, unconditional
